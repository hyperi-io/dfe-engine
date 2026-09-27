#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_issued_password_paths.py
#  Purpose:      No route reaches an issued-password account except its own change
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Two ways round the forced password change, each refused.

An account on an issued password holds no standing until its owner replaces it,
and the replacement may be neither the current password nor the one issued. The
login gate enforces that for a session the engine minted. These tests hold the two
paths that never pass through it: an admin writing the account through the
governed-ops and account CRUD routes, and a proxied request asserting the
account's name in ``X-Oidc-Subject``.
"""

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.auth import account_durability
from dfe_engine.auth.accounts import hash_password
from dfe_engine.auth.bootstrap import admin_account_name
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import ActionDef, ActionStore, PolicyStore, VarChange
from dfe_engine.settings import DFESettings

from .conftest import ADMIN_PASSWORD

_LOGIN = "/api/v1/auth/login"
_OWN_CHANGE = "/api/v1/auth/accounts/reset-password"
_ISSUED = f"issued-{secrets.token_urlsafe(16)}"
_CHOSEN = f"chosen-{secrets.token_urlsafe(16)}"
_FLAGGED = "bob"


def _wire_gitcrud(app, tmp_path: Path) -> GitCrud:
    """Attach a real local deploy repo under a dev posture, where writes commit to main."""
    app.state.settings.env = "dev"
    gc = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


def _login_status(client: TestClient, username: str, password: str) -> int:
    return client.post(_LOGIN, json={"username": username, "password": password}).status_code


@pytest.fixture
def flagged(app, client) -> str:
    """A local admin on an issued password, beside the owner the ``client`` fixture signs in."""
    app.state.account_store.create(_FLAGGED, _ISSUED, groups=["dfe-admins"], change_required=True)
    app.state.group_store.add_member("dfe-admins", _FLAGGED)
    return _FLAGGED


def _assert_still_issued(app, client: TestClient, username: str, stored_hash: str) -> None:
    account = app.state.account_store.get(username)
    assert account.password_hash == stored_hash
    assert account.password_change_required is True
    assert _login_status(client, username, _CHOSEN) == 401
    issued = client.post(_LOGIN, json={"username": username, "password": _ISSUED})
    assert issued.status_code == 200, issued.text
    assert issued.json()["password_change_required"] is True


class TestAnAdminWritingTheAccount:
    """An admin may reset the password, never to the issued one, and may not write its hash."""

    def test_an_action_cannot_write_the_hash_or_clear_the_flag(
        self, app, client, admin_headers, flagged, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        store = app.state.account_store
        account_durability.publish_direct(gc, store.get(flagged), summary="seed")
        before = gc.get("accounts", flagged)
        # Saved directly, as a hand edit to the deploy repo would be, so invoke is under test.
        ActionStore(gc).save(
            ActionDef(
                name="set-bob",
                description="write an account's credential",
                required_action="action:invoke:set-bob",
                changes=[
                    VarChange(
                        cls="accounts",
                        name=flagged,
                        path="password_hash",
                        value=hash_password(_CHOSEN),
                    ),
                    VarChange(
                        cls="accounts",
                        name=flagged,
                        path="password_change_required",
                        value=False,
                    ),
                ],
            ),
            "admin",
        )

        resp = client.post("/api/v1/governance/actions/set-bob/invoke", headers=admin_headers)

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "action_forbidden"
        assert gc.get("accounts", flagged) == before
        _assert_still_issued(app, client, flagged, before["password_hash"])

    def test_defining_that_action_is_refused(self, app, client, admin_headers, flagged, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        action = {
            "name": "set-bob",
            "description": "write an account's credential",
            "required_action": "action:invoke:set-bob",
            "changes": [
                {
                    "cls": "accounts",
                    "name": flagged,
                    "path": "password_hash",
                    "value": hash_password(_CHOSEN),
                }
            ],
        }

        resp = client.post("/api/v1/governance/admin/actions", json=action, headers=admin_headers)

        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "credential_in_action"
        assert ActionStore(gc).list() == []

    def test_the_account_update_route_ignores_the_credential_fields(
        self, app, client, admin_headers, flagged
    ):
        stored_hash = app.state.account_store.get(flagged).password_hash

        resp = client.put(
            f"/api/v1/auth/accounts/{flagged}",
            json={
                "name": "Bob",
                "password_hash": hash_password(_CHOSEN),
                "password_change_required": False,
                "seeded_password_hash": "",
            },
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Bob"
        _assert_still_issued(app, client, flagged, stored_hash)

    def test_a_scim_replace_ignores_the_password(self, app, client, admin_headers, flagged):
        stored_hash = app.state.account_store.get(flagged).password_hash

        resp = client.put(
            f"/api/v1/scim/v2/Users/{flagged}",
            json={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "userName": flagged,
                "password": _CHOSEN,
                "active": True,
            },
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        _assert_still_issued(app, client, flagged, stored_hash)

    def test_an_admin_reset_to_the_issued_password_is_refused(
        self, app, client, admin_headers, flagged
    ):
        stored_hash = app.state.account_store.get(flagged).password_hash

        resp = client.post(
            f"/api/v1/auth/accounts/{flagged}/reset-password",
            json={"new_password": _ISSUED},
            headers=admin_headers,
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "password_reused"
        _assert_still_issued(app, client, flagged, stored_hash)


def _proxied(subject: str) -> dict[str, str]:
    """What a trusted proxy forwards for an IdP identity claiming the admin group."""
    return {"X-Oidc-Subject": subject, "X-Oidc-Groups": "dfe-admins"}


@pytest.fixture
def booted(api_settings: DFESettings, request) -> Iterator[tuple[TestClient, str, str]]:
    """The app as a deployment boots it: ``(client, admin name, subject the proxy asserts)``."""
    admin_name, subject = request.param
    api_settings.auth.local.admin_name = admin_name
    app = create_app(settings=api_settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, admin_account_name(admin_name), subject
    finally:
        _registries.clear()


# (configured admin name, proxied subject that is the admin's exact name).
_NAMING_THE_ADMIN = [("", "admin"), ("ops_admin", "ops_admin"), ("Ops.Admin", "Ops.Admin")]
_NAMING_IDS = ["admin", "underscore", "dotted"]

# A subject that joins onto the admin's file rather than naming it.
_WALKING_TO_THE_ADMIN = [("", "../accounts/admin")]


class TestAProxiedIdentity:
    """A trusted proxy's subject never reaches an issued-password account."""

    @pytest.mark.parametrize("booted", _NAMING_THE_ADMIN, ids=_NAMING_IDS, indirect=True)
    def test_the_issued_admin_gets_no_standing(self, booted):
        client, name, subject = booted

        resp = client.get("/api/v1/sources", headers=_proxied(subject))

        assert resp.status_code == 401, resp.text
        assert client.app.state.account_store.get(name).password_change_required is True

    @pytest.mark.parametrize(
        "booted",
        _NAMING_THE_ADMIN + _WALKING_TO_THE_ADMIN,
        ids=[*_NAMING_IDS, "traversal"],
        indirect=True,
    )
    def test_the_issued_admins_password_cannot_be_replaced(self, booted):
        client, name, subject = booted
        store = client.app.state.account_store
        stored_hash = store.get(name).password_hash

        resp = client.post(_OWN_CHANGE, json={"new_password": _CHOSEN}, headers=_proxied(subject))

        assert store.get(name).password_hash == stored_hash, resp.text
        assert _login_status(client, name, _CHOSEN) == 401
        issued = client.post(_LOGIN, json={"username": name, "password": ADMIN_PASSWORD})
        assert issued.json()["password_change_required"] is True

    @pytest.mark.parametrize("booted", _WALKING_TO_THE_ADMIN, ids=["traversal"], indirect=True)
    def test_a_subject_that_walks_to_the_admin_is_its_own_identity(self, booted):
        client, name, subject = booted

        own = client.get("/api/v1/auth/accounts/me", headers=_proxied(subject))

        assert own.json().get("username") != name, own.text

    @pytest.mark.parametrize("booted", _NAMING_THE_ADMIN, ids=_NAMING_IDS, indirect=True)
    def test_the_refusal_writes_no_shadow_account(self, booted):
        client, _name, subject = booted
        store = client.app.state.account_store
        before = sorted(account.username for account in store.list())

        client.get("/api/v1/sources", headers=_proxied(subject))

        assert sorted(account.username for account in store.list()) == before

    @pytest.mark.parametrize("booted", [("", "")], ids=["admin"], indirect=True)
    @pytest.mark.parametrize("username", ["bob", "bob.smith", "Bob_Smith"])
    def test_a_flagged_local_account_gets_no_standing(self, booted, username):
        client, _name, _subject = booted
        store = client.app.state.account_store
        store.create(username, _ISSUED, groups=["dfe-admins"], change_required=True)
        client.app.state.group_store.add_member("dfe-admins", username)
        stored_hash = store.get(username).password_hash

        read = client.get("/api/v1/sources", headers=_proxied(username))
        change = client.post(
            _OWN_CHANGE, json={"new_password": _CHOSEN}, headers=_proxied(username)
        )

        assert read.status_code == 401, read.text
        assert change.status_code == 401, change.text
        assert store.get(username).password_hash == stored_hash
        assert store.get(username).password_change_required is True
