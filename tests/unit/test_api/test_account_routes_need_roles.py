#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_account_routes_need_roles.py
#  Purpose:      Account and API key writes that change group membership need its roles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""An account's or a key's groups hand out their roles, so writing them is assigning roles.

``mgr`` holds a custom role with ``account:*`` and ``api_key:*`` through the
``account-managers`` group, and neither ``admin`` nor ``role:write``.
"""

import secrets

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token

ACCOUNTS = "/api/v1/auth/accounts"
KEYS = "/api/v1/auth/api-keys"
_PASSWORD = secrets.token_urlsafe(16)


def _bearer(api_settings, sub: str) -> dict[str, str]:
    token = create_access_token(data={"sub": sub}, settings=api_settings)
    return {"Authorization": f"Bearer {token}"}


def _roles(client: TestClient, headers: dict[str, str]) -> list[str]:
    resp = client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["roles"]


@pytest.fixture
def mgr(client: TestClient, app, api_settings) -> dict[str, str]:
    """Headers for an account manager that holds neither admin nor role:write."""
    app.state.role_store.create(
        "account_manager", description="accounts and keys", permissions=["account:*", "api_key:*"]
    )
    app.state.role_config = app.state.role_store.load_config()
    app.state.account_store.create("mgr", _PASSWORD, groups=["account-managers"])
    app.state.group_store.create("account-managers", ["account_manager"], members=["mgr"])
    headers = _bearer(api_settings, "mgr")
    assert _roles(client, headers) == ["account_manager"]
    return headers


class TestAnAccountsGroupsNeedTheirRoles:
    def test_creating_an_account_in_the_admin_group_is_refused(self, client, app, mgr):
        resp = client.post(
            ACCOUNTS,
            json={"username": "mallory", "password": _PASSWORD, "groups": ["dfe-admins"]},
            headers=mgr,
        )

        assert resp.status_code == 403, resp.text
        assert app.state.account_store.get("mallory") is None
        assert "mallory" not in app.state.group_store.get("dfe-admins").members

    def test_putting_an_account_into_the_admin_group_is_refused(self, client, app, mgr):
        resp = client.put(
            f"{ACCOUNTS}/viewer", json={"groups": ["dfe-viewers", "dfe-admins"]}, headers=mgr
        )

        assert resp.status_code == 403, resp.text
        assert "viewer" not in app.state.group_store.get("dfe-admins").members
        assert app.state.account_store.get("viewer").groups == ["dfe-viewers"]

    def test_taking_an_account_out_of_the_admin_group_is_refused(self, client, app, mgr):
        app.state.account_store.create("ops", _PASSWORD, groups=["dfe-admins"])
        app.state.group_store.add_member("dfe-admins", "ops")

        resp = client.put(f"{ACCOUNTS}/ops", json={"groups": []}, headers=mgr)

        assert resp.status_code == 403, resp.text
        assert "ops" in app.state.group_store.get("dfe-admins").members

    def test_an_idp_accounts_own_list_is_checked_too(self, client, app, mgr):
        """An IdP-owned account holds the groups its record names, whatever the group files say."""
        app.state.account_store.create("jane-corp-com", "", groups=["dfe-viewers"])
        app.state.account_store.update("jane-corp-com", external=True, source_provider="entra")

        resp = client.put(f"{ACCOUNTS}/jane-corp-com", json={"groups": ["dfe-admins"]}, headers=mgr)

        assert resp.status_code == 403, resp.text
        assert app.state.account_store.get("jane-corp-com").groups == ["dfe-viewers"]

    def test_naming_a_held_group_by_its_other_identifier_is_no_change(self, client, app, mgr):
        """A group held by provider id and re-sent by name grants nothing new."""
        app.state.group_store.update("dfe-admins", source_id="entra-admins-guid")
        app.state.account_store.create("jane-corp-com", "", groups=["entra-admins-guid"])
        app.state.account_store.update("jane-corp-com", external=True, source_provider="entra")

        resp = client.put(f"{ACCOUNTS}/jane-corp-com", json={"groups": ["dfe-admins"]}, headers=mgr)

        assert resp.status_code == 200, resp.text

    def test_groups_whose_roles_the_caller_holds_are_accepted(self, client, app, mgr):
        resp = client.post(
            ACCOUNTS,
            json={"username": "helper", "password": _PASSWORD, "groups": ["account-managers"]},
            headers=mgr,
        )

        assert resp.status_code == 201, resp.text
        assert "helper" in app.state.group_store.get("account-managers").members

    def test_admin_still_puts_an_account_into_the_admin_group(self, client, app, admin_headers):
        resp = client.post(
            ACCOUNTS,
            json={"username": "second", "password": _PASSWORD, "groups": ["dfe-admins"]},
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert "second" in app.state.group_store.get("dfe-admins").members


class TestAKeysGroupsNeedTheirRoles:
    @pytest.mark.parametrize("by", ["name", "source_id"])
    def test_minting_a_key_in_the_admin_group_is_refused(self, client, app, mgr, by):
        """A key's groups resolve by name, else by the provider id the sync recorded."""
        app.state.group_store.update("dfe-admins", source_id="entra-admins-guid")
        group = "dfe-admins" if by == "name" else "entra-admins-guid"

        resp = client.post(KEYS, json={"name": "ci-escalate", "groups": [group]}, headers=mgr)

        assert resp.status_code == 403, resp.text
        assert app.state.api_key_store.get("ci-escalate") is None

    def test_a_key_in_groups_whose_roles_the_caller_holds_is_minted(self, client, mgr):
        resp = client.post(
            KEYS, json={"name": "ci-helper", "groups": ["account-managers"]}, headers=mgr
        )

        assert resp.status_code == 201, resp.text

    def test_admin_still_mints_an_admin_key(self, client, admin_headers):
        resp = client.post(
            KEYS, json={"name": "ci-admin", "groups": ["dfe-admins"]}, headers=admin_headers
        )

        assert resp.status_code == 201, resp.text


class TestADeletedAccountLeavesItsGroups:
    """Membership is keyed by name, so an account recreated under a deleted name must start empty."""

    @pytest.fixture
    def ghost(self, app) -> None:
        app.state.account_store.create("ghost", _PASSWORD, groups=["dfe-admins"])
        app.state.group_store.add_member("dfe-admins", "ghost")

    @pytest.mark.parametrize(
        "path",
        [f"{ACCOUNTS}/ghost", "/api/v1/scim/v2/Users/ghost"],
        ids=["account-route", "scim"],
    )
    def test_a_recreated_account_inherits_nothing(
        self, client, app, admin_headers, api_settings, ghost, path
    ):
        deleted = client.delete(path, headers=admin_headers)
        recreated = client.post(
            ACCOUNTS, json={"username": "ghost", "password": _PASSWORD}, headers=admin_headers
        )

        assert deleted.status_code == 204, deleted.text
        assert recreated.status_code == 201, recreated.text
        assert "ghost" not in app.state.group_store.get("dfe-admins").members
        assert _roles(client, _bearer(api_settings, "ghost")) == []
