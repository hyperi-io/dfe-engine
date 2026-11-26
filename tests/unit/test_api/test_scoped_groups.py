#  Project:      dfe-engine
#  File:         test_scoped_groups.py
#  Purpose:      API-level scoped RBAC: org group visibility + org-local admin
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Scoped RBAC through the API.

Proves three properties:
- the API creates only system-wide groups, so a body naming a scope is refused,
- org-scoped grants never leak outside their org (read/write), and
- org-local groups are invisible to everyone outside the org, while
  members always see the groups they belong to.
"""

import pytest
from fastapi.testclient import TestClient

from dfe_engine.settings import DFESettings
from tests.support.accounts import session_token
from tests.support.org_groups import seed_org_group

GROUPS = "/api/v1/auth/groups"
ORGS = "/api/v1/orgs"


def _headers(app, api_settings: DFESettings, username: str) -> dict[str, str]:
    token = session_token(app, api_settings, {"sub": username, "org_id": "default"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def scoped_setup(client: TestClient, app, admin_headers: dict[str, str]):
    """Two orgs. An org-local admin group and analyst group in acme."""
    account_store = app.state.account_store

    for org in ("acme", "globex"):
        resp = client.post(ORGS, json={"name": org}, headers=admin_headers)
        assert resp.status_code == 201, resp.text

    for username in ("orgadmin", "orguser"):
        if account_store.get(username) is None:
            account_store.create(username, f"pw-{username}")

    for name, roles, members, scope in (
        ("acme-ops", ["admin"], ["orgadmin"], "org:acme"),
        ("acme-analysts", ["data_analyst"], ["orguser"], "org:acme"),
        ("globex-analysts", ["data_analyst"], [], "org:globex"),
    ):
        seed_org_group(app=app, members=members, name=name, roles=roles, scope=scope)
    return client


class TestOrgGroupVisibility:
    def test_admin_sees_all_groups_with_scope(self, scoped_setup, admin_headers):
        resp = scoped_setup.get(GROUPS, headers=admin_headers)
        assert resp.status_code == 200
        by_name = {g["name"]: g for g in resp.json()["items"]}
        assert by_name["acme-analysts"]["scope"] == "org:acme"
        assert by_name["dfe-admins"]["scope"] == "system"
        assert "globex-analysts" in by_name

    def test_member_sees_only_own_groups(self, scoped_setup, api_settings):
        resp = scoped_setup.get(GROUPS, headers=_headers(scoped_setup.app, api_settings, "orguser"))
        assert resp.status_code == 200
        assert [g["name"] for g in resp.json()["items"]] == ["acme-analysts"]

    def test_org_group_invisible_cross_org(self, scoped_setup, api_settings):
        headers = _headers(scoped_setup.app, api_settings, "orgadmin")
        resp = scoped_setup.get(f"{GROUPS}/globex-analysts", headers=headers)
        assert resp.status_code == 404

    def test_system_group_invisible_without_grant(self, scoped_setup, api_settings):
        resp = scoped_setup.get(
            f"{GROUPS}/dfe-admins", headers=_headers(scoped_setup.app, api_settings, "orguser")
        )
        assert resp.status_code == 404

    def test_org_admin_sees_own_org_groups(self, scoped_setup, api_settings):
        resp = scoped_setup.get(
            GROUPS, headers=_headers(scoped_setup.app, api_settings, "orgadmin")
        )
        assert resp.status_code == 200
        names = {g["name"] for g in resp.json()["items"]}
        # Creating acme made its admin and viewer groups too.
        assert names == {"acme-admins", "acme-analysts", "acme-ops", "acme-viewers"}


class TestOrgLocalAdmin:
    def test_can_update_and_delete_own_org_groups(self, scoped_setup, api_settings):
        seed_org_group(
            app=scoped_setup.app, name="acme-eng", roles=["data_viewer"], scope="org:acme"
        )
        headers = _headers(scoped_setup.app, api_settings, "orgadmin")

        resp = scoped_setup.put(
            f"{GROUPS}/acme-eng", json={"roles": ["data_analyst"]}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["roles"] == ["data_analyst"]

        resp = scoped_setup.delete(f"{GROUPS}/acme-eng", headers=headers)
        assert resp.status_code == 204

    def test_cannot_create_a_group(self, scoped_setup, api_settings):
        """Every group the API creates is system-wide, which an org admin cannot write."""
        resp = scoped_setup.post(
            GROUPS,
            json={"name": "sneaky", "roles": ["admin"]},
            headers=_headers(scoped_setup.app, api_settings, "orgadmin"),
        )
        assert resp.status_code == 403
        assert scoped_setup.app.state.group_store.get("sneaky") is None

    def test_cannot_modify_system_group(self, scoped_setup, api_settings):
        """A system group is invisible to an org admin, so it answers as a missing one."""
        resp = scoped_setup.put(
            f"{GROUPS}/dfe-viewers",
            json={"roles": ["admin"]},
            headers=_headers(scoped_setup.app, api_settings, "orgadmin"),
        )
        assert resp.status_code == 404

    def test_org_wildcard_does_not_reach_system_endpoints(self, scoped_setup, api_settings):
        resp = scoped_setup.post(
            ORGS,
            json={"name": "evil"},
            headers=_headers(scoped_setup.app, api_settings, "orgadmin"),
        )
        assert resp.status_code == 403


class TestOrgReads:
    def test_org_admin_lists_only_own_org(self, scoped_setup, api_settings):
        resp = scoped_setup.get(ORGS, headers=_headers(scoped_setup.app, api_settings, "orgadmin"))
        assert resp.status_code == 200
        assert [o["name"] for o in resp.json()["items"]] == ["acme"]

    def test_org_admin_reads_own_org_not_others(self, scoped_setup, api_settings):
        headers = _headers(scoped_setup.app, api_settings, "orgadmin")
        assert scoped_setup.get(f"{ORGS}/acme", headers=headers).status_code == 200
        assert scoped_setup.get(f"{ORGS}/globex", headers=headers).status_code == 403

    def test_system_viewer_lists_all_orgs(self, scoped_setup, viewer_headers):
        # data_analyst_viewer (system scope via dfe-viewers) carries org:read
        resp = scoped_setup.get(ORGS, headers=viewer_headers)
        assert resp.status_code == 200
        assert {o["name"] for o in resp.json()["items"]} == {"acme", "globex"}


class TestScopeValidation:
    @pytest.mark.parametrize(
        "scope", ["system", "org:acme", "everywhere", "org:../elsewhere/outside"]
    )
    def test_a_body_naming_a_scope_is_refused(self, scoped_setup, app, admin_headers, scope):
        resp = scoped_setup.post(
            GROUPS, json={"name": "scoped", "roles": [], "scope": scope}, headers=admin_headers
        )

        assert resp.status_code == 422, resp.text
        assert app.state.group_store.get("scoped") is None

    def test_viewer_sees_own_membership_only(self, scoped_setup, viewer_headers):
        # dfe-viewers roles carry no group:read - visibility is membership only.
        resp = scoped_setup.get(GROUPS, headers=viewer_headers)
        assert resp.status_code == 200
        assert [g["name"] for g in resp.json()["items"]] == ["dfe-viewers"]
