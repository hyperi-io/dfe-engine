#  Project:      dfe-engine
#  File:         test_scoped_groups.py
#  Purpose:      API-level scoped RBAC: org group visibility + org-local admin
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Scoped RBAC through the API.

Proves the two load-bearing properties:
- org-scoped grants never leak outside their org (create/read/write), and
- org-local groups are invisible to everyone outside the org, while
  members always see the groups they belong to.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.settings import DFESettings

GROUPS = "/api/v1/auth/groups"
ORGS = "/api/v1/orgs"


def _headers(api_settings: DFESettings, username: str) -> dict[str, str]:
    token = create_access_token(data={"sub": username, "org_id": "default"}, settings=api_settings)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def scoped_setup(client: TestClient, app, admin_headers: dict[str, str]):
    """Two orgs; an org-local admin group and analyst group in acme."""
    account_store = app.state.account_store

    for org in ("acme", "globex"):
        resp = client.post(ORGS, json={"name": org}, headers=admin_headers)
        assert resp.status_code == 201, resp.text

    for username in ("orgadmin", "orguser"):
        if account_store.get(username) is None:
            account_store.create(username, f"pw-{username}")

    for name, roles, members in (
        ("acme-admins", ["admin"], ["orgadmin"]),
        ("acme-analysts", ["data_analyst"], ["orguser"]),
    ):
        resp = client.post(
            GROUPS,
            json={"name": name, "roles": roles, "scope": "org:acme", "members": members},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text

    resp = client.post(
        GROUPS,
        json={"name": "globex-analysts", "roles": ["data_analyst"], "scope": "org:globex"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return client


class TestOrgGroupVisibility:
    def test_admin_sees_all_groups_with_scope(self, scoped_setup, admin_headers):
        resp = scoped_setup.get(GROUPS, headers=admin_headers)
        assert resp.status_code == 200
        by_name = {g["name"]: g for g in resp.json()}
        assert by_name["acme-analysts"]["scope"] == "org:acme"
        assert by_name["dfe-admins"]["scope"] == "system"
        assert "globex-analysts" in by_name

    def test_member_sees_only_own_groups(self, scoped_setup, api_settings):
        resp = scoped_setup.get(GROUPS, headers=_headers(api_settings, "orguser"))
        assert resp.status_code == 200
        assert [g["name"] for g in resp.json()] == ["acme-analysts"]

    def test_org_group_invisible_cross_org(self, scoped_setup, api_settings):
        headers = _headers(api_settings, "orgadmin")
        resp = scoped_setup.get(f"{GROUPS}/globex-analysts", headers=headers)
        assert resp.status_code == 404

    def test_system_group_invisible_without_grant(self, scoped_setup, api_settings):
        resp = scoped_setup.get(f"{GROUPS}/dfe-admins", headers=_headers(api_settings, "orguser"))
        assert resp.status_code == 404

    def test_org_admin_sees_own_org_groups(self, scoped_setup, api_settings):
        resp = scoped_setup.get(GROUPS, headers=_headers(api_settings, "orgadmin"))
        assert resp.status_code == 200
        names = {g["name"] for g in resp.json()}
        assert names == {"acme-admins", "acme-analysts"}


class TestOrgLocalAdmin:
    def test_can_crud_own_org_groups(self, scoped_setup, api_settings):
        headers = _headers(api_settings, "orgadmin")
        resp = scoped_setup.post(
            GROUPS,
            json={"name": "acme-eng", "roles": ["data_viewer"], "scope": "org:acme"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text

        resp = scoped_setup.put(
            f"{GROUPS}/acme-eng", json={"roles": ["data_analyst"]}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["roles"] == ["data_analyst"]

        resp = scoped_setup.delete(f"{GROUPS}/acme-eng", headers=headers)
        assert resp.status_code == 204

    def test_cannot_create_system_group(self, scoped_setup, api_settings):
        resp = scoped_setup.post(
            GROUPS,
            json={"name": "sneaky", "roles": ["admin"], "scope": "system"},
            headers=_headers(api_settings, "orgadmin"),
        )
        assert resp.status_code == 403

    def test_cannot_create_group_in_other_org(self, scoped_setup, api_settings):
        resp = scoped_setup.post(
            GROUPS,
            json={"name": "sneaky", "roles": ["admin"], "scope": "org:globex"},
            headers=_headers(api_settings, "orgadmin"),
        )
        assert resp.status_code == 403

    def test_cannot_modify_system_group(self, scoped_setup, api_settings):
        resp = scoped_setup.put(
            f"{GROUPS}/dfe-viewers",
            json={"roles": ["admin"]},
            headers=_headers(api_settings, "orgadmin"),
        )
        assert resp.status_code == 403

    def test_org_wildcard_does_not_reach_system_endpoints(self, scoped_setup, api_settings):
        resp = scoped_setup.post(
            ORGS, json={"name": "evil"}, headers=_headers(api_settings, "orgadmin")
        )
        assert resp.status_code == 403


class TestOrgReads:
    def test_org_admin_lists_only_own_org(self, scoped_setup, api_settings):
        resp = scoped_setup.get(ORGS, headers=_headers(api_settings, "orgadmin"))
        assert resp.status_code == 200
        assert [o["name"] for o in resp.json()] == ["acme"]

    def test_org_admin_reads_own_org_not_others(self, scoped_setup, api_settings):
        headers = _headers(api_settings, "orgadmin")
        assert scoped_setup.get(f"{ORGS}/acme", headers=headers).status_code == 200
        assert scoped_setup.get(f"{ORGS}/globex", headers=headers).status_code == 403

    def test_system_viewer_lists_all_orgs(self, scoped_setup, viewer_headers):
        # data_analyst_ro (system scope via dfe-viewers) carries org:read
        resp = scoped_setup.get(ORGS, headers=viewer_headers)
        assert resp.status_code == 200
        assert {o["name"] for o in resp.json()} == {"acme", "globex"}


class TestScopeValidation:
    def test_invalid_scope_string(self, scoped_setup, admin_headers):
        resp = scoped_setup.post(
            GROUPS,
            json={"name": "bad", "roles": [], "scope": "everywhere"},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_unknown_org_rejected(self, scoped_setup, admin_headers):
        resp = scoped_setup.post(
            GROUPS,
            json={"name": "bad", "roles": [], "scope": "org:nonexistent"},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_viewer_sees_own_membership_only(self, scoped_setup, viewer_headers):
        # dfe-viewers roles carry no group:read - visibility is membership only.
        resp = scoped_setup.get(GROUPS, headers=viewer_headers)
        assert resp.status_code == 200
        assert [g["name"] for g in resp.json()] == ["dfe-viewers"]
