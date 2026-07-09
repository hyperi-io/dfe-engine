#  Project:      dfe-engine
#  File:         test_repository_api.py
#  Purpose:      Repository API - preferences merge + scoped-object RBAC matrix
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Repository endpoints through the API with a fake ClickHouse client.

Covers the preferences layering (system -> org -> group -> user), the
merge-patch fast path with etags, and the scoped-object RBAC matrix
(membership reads, owner writes, org-wildcard admins, 404-not-403 hiding).
"""

from __future__ import annotations

import pytest
from common.fake_repository_ch import FakeRepositoryCH
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token, get_clickhouse_client
from dfe_engine.repository.store import RepositoryStore
from dfe_engine.settings import DFESettings

PREFS = "/api/v1/repository/preferences"
OBJECTS = "/api/v1/repository/objects"
GROUPS = "/api/v1/auth/groups"
ORGS = "/api/v1/orgs"


def _headers(api_settings: DFESettings, username: str) -> dict[str, str]:
    token = create_access_token(data={"sub": username, "org_id": "default"}, settings=api_settings)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _reset_schema_flag():
    RepositoryStore._ensured_databases.clear()
    yield
    RepositoryStore._ensured_databases.clear()


@pytest.fixture
def fake_ch(app) -> FakeRepositoryCH:
    ch = FakeRepositoryCH()
    app.dependency_overrides[get_clickhouse_client] = lambda: ch
    yield ch
    app.dependency_overrides.pop(get_clickhouse_client, None)


@pytest.fixture
def repo_setup(client: TestClient, app, fake_ch, admin_headers: dict[str, str]) -> TestClient:
    """Two orgs; org-local admin + analyst groups in acme, analysts in globex."""
    account_store = app.state.account_store

    for org in ("acme", "globex"):
        resp = client.post(ORGS, json={"name": org}, headers=admin_headers)
        assert resp.status_code == 201, resp.text

    for username in ("orgadmin", "orguser", "globexuser"):
        if account_store.get(username) is None:
            account_store.create(username, f"pw-{username}")

    for name, roles, scope, members in (
        ("acme-admins", ["admin"], "org:acme", ["orgadmin"]),
        ("acme-analysts", ["data_analyst"], "org:acme", ["orguser"]),
        ("globex-analysts", ["data_analyst"], "org:globex", ["globexuser"]),
    ):
        resp = client.post(
            GROUPS,
            json={"name": name, "roles": roles, "scope": scope, "members": members},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
    return client


# ── Preferences ──────────────────────────────────────────────


class TestPreferences:
    def test_get_empty(self, client: TestClient, fake_ch, api_settings):
        resp = client.get(PREFS, headers=_headers(api_settings, "user1"))
        assert resp.status_code == 200
        assert resp.json() == {"preferences": {}, "etag": None}

    def test_requires_auth(self, client: TestClient, fake_ch):
        assert client.get(PREFS).status_code == 401

    def test_layered_merge_and_user_patch(self, repo_setup, admin_headers, api_settings):
        client = repo_setup
        # Admin writes the system layer through the objects endpoint.
        resp = client.put(
            f"{OBJECTS}/system/-/preferences/default",
            json={"theme": "light", "brand": "hyperi"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("ETag")
        # Org layer overrides the theme for acme members.
        resp = client.put(
            f"{OBJECTS}/org/acme/preferences/default",
            json={"theme": "corporate"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text

        orguser = _headers(api_settings, "orguser")
        resp = client.get(PREFS, headers=orguser)
        assert resp.status_code == 200
        assert resp.json()["preferences"] == {"theme": "corporate", "brand": "hyperi"}
        assert resp.json()["etag"] is None  # no user layer yet

        # The theme toggle: one PATCH with a merge patch.
        resp = client.patch(PREFS, json={"theme": "dark"}, headers=orguser)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["preferences"] == {"theme": "dark", "brand": "hyperi"}
        assert body["etag"]

        resp = client.get(PREFS, headers=orguser)
        assert resp.json()["preferences"] == {"theme": "dark", "brand": "hyperi"}

        # null removes the user key -> falls back to the inherited org value.
        resp = client.patch(PREFS, json={"theme": None}, headers=orguser)
        assert resp.status_code == 200
        assert resp.json()["preferences"] == {"theme": "corporate", "brand": "hyperi"}

    def test_if_match_mismatch_412(self, client: TestClient, fake_ch, api_settings):
        user1 = _headers(api_settings, "user1")
        resp = client.patch(PREFS, json={"a": 1}, headers=user1)
        assert resp.status_code == 200
        etag = resp.json()["etag"]
        assert etag

        resp = client.patch(PREFS, json={"a": 2}, headers={**user1, "If-Match": "bogus-etag"})
        assert resp.status_code == 412
        assert resp.json()["code"] == "precondition_failed"
        assert resp.headers["ETag"] == etag

        resp = client.patch(PREFS, json={"a": 2}, headers={**user1, "If-Match": etag})
        assert resp.status_code == 200
        assert resp.json()["preferences"] == {"a": 2}

    def test_prefs_size_cap_413(self, client: TestClient, app, fake_ch, api_settings):
        app.state.settings.repository.max_prefs_bytes = 32
        resp = client.patch(PREFS, json={"big": "x" * 100}, headers=_headers(api_settings, "user1"))
        assert resp.status_code == 413
        assert resp.json()["code"] == "payload_too_large"


# ── Objects RBAC matrix ──────────────────────────────────────


class TestObjectsRbac:
    def test_org_member_reads_own_org_object(self, repo_setup, admin_headers, api_settings):
        client = repo_setup
        resp = client.put(
            f"{OBJECTS}/org/acme/ui/welcome",
            content=b"hello acme",
            headers={**admin_headers, "Content-Type": "text/plain"},
        )
        assert resp.status_code == 200, resp.text
        resp = client.get(
            f"{OBJECTS}/org/acme/ui/welcome", headers=_headers(api_settings, "orguser")
        )
        assert resp.status_code == 200
        assert resp.content == b"hello acme"
        assert resp.headers["Content-Type"].startswith("text/plain")
        assert resp.headers.get("ETag")

    def test_cross_org_read_hidden(self, repo_setup, admin_headers, api_settings):
        client = repo_setup
        client.put(f"{OBJECTS}/org/acme/ui/welcome", content=b"hello", headers=admin_headers)
        resp = client.get(
            f"{OBJECTS}/org/acme/ui/welcome", headers=_headers(api_settings, "globexuser")
        )
        assert resp.status_code == 404

    def test_cross_org_write_forbidden(self, repo_setup, api_settings):
        resp = repo_setup.put(
            f"{OBJECTS}/org/acme/ui/welcome",
            content=b"nope",
            headers=_headers(api_settings, "globexuser"),
        )
        assert resp.status_code == 403

    def test_non_admin_cannot_put_system(self, repo_setup, api_settings):
        resp = repo_setup.put(
            f"{OBJECTS}/system/-/ui/banner",
            content=b"nope",
            headers=_headers(api_settings, "user1"),
        )
        assert resp.status_code == 403

    def test_system_readable_by_anyone(self, repo_setup, admin_headers, api_settings):
        client = repo_setup
        assert (
            client.put(
                f"{OBJECTS}/system/-/ui/banner", content=b"welcome", headers=admin_headers
            ).status_code
            == 200
        )
        resp = client.get(f"{OBJECTS}/system/-/ui/banner", headers=_headers(api_settings, "user1"))
        assert resp.status_code == 200
        assert resp.content == b"welcome"

    def test_owner_rw_own_user_object(self, client: TestClient, fake_ch, api_settings):
        user1 = _headers(api_settings, "user1")
        resp = client.put(f"{OBJECTS}/user/user1/notes/todo", content=b"do it", headers=user1)
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"] == "user"
        assert body["size"] == len(b"do it")
        assert client.get(f"{OBJECTS}/user/user1/notes/todo", headers=user1).status_code == 200
        assert client.delete(f"{OBJECTS}/user/user1/notes/todo", headers=user1).status_code == 204
        assert client.get(f"{OBJECTS}/user/user1/notes/todo", headers=user1).status_code == 404

    def test_other_users_object_hidden(self, client: TestClient, fake_ch, api_settings):
        user1 = _headers(api_settings, "user1")
        user2 = _headers(api_settings, "user2")
        client.put(f"{OBJECTS}/user/user1/notes/todo", content=b"secret", headers=user1)
        assert client.get(f"{OBJECTS}/user/user1/notes/todo", headers=user2).status_code == 404
        assert (
            client.put(
                f"{OBJECTS}/user/user1/notes/todo", content=b"hack", headers=user2
            ).status_code
            == 403
        )
        assert client.delete(f"{OBJECTS}/user/user1/notes/todo", headers=user2).status_code == 403

    def test_admin_reads_other_users_object(self, repo_setup, admin_headers, api_settings):
        client = repo_setup
        client.put(
            f"{OBJECTS}/user/user1/notes/todo",
            content=b"secret",
            headers=_headers(api_settings, "user1"),
        )
        assert (
            client.get(f"{OBJECTS}/user/user1/notes/todo", headers=admin_headers).status_code == 200
        )

    def test_org_wildcard_admin_scope_fence(self, repo_setup, api_settings):
        client = repo_setup
        orgadmin = _headers(api_settings, "orgadmin")
        # Can write the own-org layer...
        assert (
            client.put(
                f"{OBJECTS}/org/acme/ui/theme", content=b"acme-theme", headers=orgadmin
            ).status_code
            == 200
        )
        # ...but not the system layer, and not another org.
        assert (
            client.put(
                f"{OBJECTS}/system/-/ui/theme", content=b"nope", headers=orgadmin
            ).status_code
            == 403
        )
        assert (
            client.put(
                f"{OBJECTS}/org/globex/ui/theme", content=b"nope", headers=orgadmin
            ).status_code
            == 403
        )

    def test_group_object_readable_by_member(self, repo_setup, admin_headers, api_settings):
        client = repo_setup
        resp = client.put(
            f"{OBJECTS}/group/acme-analysts/ui/dashboard",
            json={"layout": "grid"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        resp = client.get(
            f"{OBJECTS}/group/acme-analysts/ui/dashboard",
            headers=_headers(api_settings, "orguser"),
        )
        assert resp.status_code == 200
        assert resp.json() == {"layout": "grid"}
        # Non-member outside the owning org: hidden.
        resp = client.get(
            f"{OBJECTS}/group/acme-analysts/ui/dashboard",
            headers=_headers(api_settings, "globexuser"),
        )
        assert resp.status_code == 404

    def test_unknown_group_404(self, repo_setup, admin_headers):
        resp = repo_setup.get(f"{OBJECTS}/group/ghost/ui/dashboard", headers=admin_headers)
        assert resp.status_code == 404

    def test_list_metadata(self, repo_setup, admin_headers, api_settings):
        client = repo_setup
        client.put(f"{OBJECTS}/org/acme/ui/a", content=b"aa", headers=admin_headers)
        client.put(f"{OBJECTS}/org/acme/ui/b", content=b"bbb", headers=admin_headers)
        resp = client.get(f"{OBJECTS}/org/acme/ui", headers=_headers(api_settings, "orguser"))
        assert resp.status_code == 200
        entries = resp.json()
        assert [e["key"] for e in entries] == ["a", "b"]
        assert entries[1]["size"] == 3
        # Cross-org list is hidden too.
        resp = client.get(f"{OBJECTS}/org/acme/ui", headers=_headers(api_settings, "globexuser"))
        assert resp.status_code == 404

    def test_put_if_match_conflict_412(self, client: TestClient, fake_ch, api_settings):
        user1 = _headers(api_settings, "user1")
        resp = client.put(f"{OBJECTS}/user/user1/notes/todo", content=b"v1", headers=user1)
        etag = resp.headers["ETag"]
        resp = client.put(
            f"{OBJECTS}/user/user1/notes/todo",
            content=b"v2",
            headers={**user1, "If-Match": "stale"},
        )
        assert resp.status_code == 412
        assert resp.json()["code"] == "precondition_failed"
        assert resp.headers["ETag"] == etag
        resp = client.put(
            f"{OBJECTS}/user/user1/notes/todo",
            content=b"v2",
            headers={**user1, "If-Match": etag},
        )
        assert resp.status_code == 200


# ── Validation + size caps ───────────────────────────────────


class TestValidation:
    def test_invalid_scope_422(self, client: TestClient, fake_ch, admin_headers):
        resp = client.get(f"{OBJECTS}/cosmic/x/ns/key", headers=admin_headers)
        assert resp.status_code == 422

    def test_system_scope_id_must_be_dash(self, client: TestClient, fake_ch, admin_headers):
        resp = client.put(f"{OBJECTS}/system/oops/ns/key", content=b"v", headers=admin_headers)
        assert resp.status_code == 422
        resp = client.get(f"{OBJECTS}/system/oops/ns/key", headers=admin_headers)
        assert resp.status_code == 422

    @pytest.mark.parametrize("namespace", [".hidden", "bad space", "-lead", "a" * 129])
    def test_invalid_namespace_422(self, client: TestClient, fake_ch, admin_headers, namespace):
        resp = client.get(f"{OBJECTS}/system/-/{namespace}", headers=admin_headers)
        assert resp.status_code == 422

    def test_invalid_key_422(self, client: TestClient, fake_ch, admin_headers):
        resp = client.put(f"{OBJECTS}/system/-/ns/bad key", content=b"v", headers=admin_headers)
        assert resp.status_code == 422

    def test_object_size_cap_413(self, client: TestClient, app, fake_ch, admin_headers):
        app.state.settings.repository.max_object_bytes = 16
        resp = client.put(f"{OBJECTS}/system/-/ns/big", content=b"x" * 64, headers=admin_headers)
        assert resp.status_code == 413
        assert resp.json()["code"] == "payload_too_large"

    def test_delete_absent_404(self, client: TestClient, fake_ch, admin_headers):
        resp = client.delete(f"{OBJECTS}/system/-/ns/ghost", headers=admin_headers)
        assert resp.status_code == 404

    def test_default_content_type(self, client: TestClient, fake_ch, api_settings):
        user1 = _headers(api_settings, "user1")
        resp = client.put(
            f"{OBJECTS}/user/user1/blobs/raw",
            content=b"\x00\x01binary",
            headers={**user1, "Content-Type": ""},
        )
        assert resp.status_code == 200
        resp = client.get(f"{OBJECTS}/user/user1/blobs/raw", headers=user1)
        assert resp.headers["Content-Type"].startswith("application/octet-stream")
        assert resp.content == b"\x00\x01binary"
