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
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.auth.breakglass import GROUP as BREAKGLASS_GROUP
from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS_USERNAME
from dfe_engine.settings import DFESettings

ACCOUNTS = "/api/v1/auth/accounts"
KEYS = "/api/v1/auth/api-keys"
SCIM_USERS = "/api/v1/scim/v2/Users"
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

    def test_a_name_that_is_a_linked_groups_source_id_is_checked_as_that_group(
        self, api_settings: DFESettings, app: FastAPI, client: TestClient, mgr: dict[str, str]
    ):
        # An IdP-owned account's own list is also its assertion, so a roleless group named like an admin group's source ID links the account to that admin group.
        app.state.group_store.update(name="dfe-admins", source_id="DFE-Admins")
        app.state.group_store.create(members=[], name="DFE-Admins", roles=[])
        app.state.account_store.create(
            groups=["dfe-viewers"], password="", username="jane-corp-com"
        )
        app.state.account_store.update(
            external=True, source_provider="entra", username="jane-corp-com"
        )

        resp = client.put(f"{ACCOUNTS}/jane-corp-com", headers=mgr, json={"groups": ["DFE-Admins"]})

        assert resp.status_code == 403, resp.text
        assert app.state.account_store.get("jane-corp-com").groups == ["dfe-viewers"]
        jane = _bearer(api_settings=api_settings, sub="jane-corp-com")
        assert "admin" not in _roles(client=client, headers=jane)

    def test_a_roleless_group_is_accepted_for_an_idp_account(
        self, app: FastAPI, client: TestClient, mgr: dict[str, str]
    ):
        app.state.group_store.create(members=[], name="DFE-Admins", roles=[])
        app.state.account_store.create(
            groups=["dfe-viewers"], password="", username="jane-corp-com"
        )
        app.state.account_store.update(
            external=True, source_provider="entra", username="jane-corp-com"
        )

        resp = client.put(f"{ACCOUNTS}/jane-corp-com", headers=mgr, json={"groups": ["DFE-Admins"]})

        assert resp.status_code == 200, resp.text
        assert app.state.account_store.get("jane-corp-com").groups == ["DFE-Admins"]

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
    def test_minting_a_key_in_the_admin_group_is_refused(self, client, app, mgr):
        """A key's groups resolve by name, as a session's do."""
        app.state.group_store.update("dfe-admins", source_id="entra-admins-guid")

        resp = client.post(
            KEYS, json={"name": "ci-escalate", "groups": ["dfe-admins"]}, headers=mgr
        )

        assert resp.status_code == 403, resp.text
        assert app.state.api_key_store.get("ci-escalate") is None

    def test_a_key_naming_a_provider_id_is_minted_and_holds_no_roles(
        self, app: FastAPI, client: TestClient, mgr: dict[str, str]
    ):
        app.state.group_store.update(name="dfe-admins", source_id="entra-admins-guid")

        resp = client.post(
            KEYS, headers=mgr, json={"groups": ["entra-admins-guid"], "name": "ci-guid"}
        )

        assert resp.status_code == 201, resp.text
        key = {"X-API-Key": resp.json()["full_key"]}
        assert _roles(client=client, headers=key) == []

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


_DELETE_ROUTES = pytest.mark.parametrize("route", [ACCOUNTS, SCIM_USERS], ids=["native", "scim"])


class TestDeletingAnAccountNeedsItsGroupsRoles:
    """Deleting an account takes it out of every group it holds, as removing it would."""

    @_DELETE_ROUTES
    def test_deleting_an_admin_is_refused(self, client, app, mgr, route):
        app.state.account_store.create("ops", _PASSWORD, groups=["dfe-admins"])
        app.state.group_store.add_member("dfe-admins", "ops")

        resp = client.delete(f"{route}/ops", headers=mgr)

        assert resp.status_code == 403, resp.text
        assert app.state.account_store.get("ops") is not None
        assert "ops" in app.state.group_store.get("dfe-admins").members

    @_DELETE_ROUTES
    def test_deleting_an_idp_admin_is_refused(self, client, app, mgr, route):
        """An IdP-owned account holds the groups its own list's ids are linked to."""
        app.state.group_store.update("dfe-admins", source_id="dfe-admins")
        app.state.account_store.create("jane-corp-com", "", groups=["dfe-admins"])
        app.state.account_store.update("jane-corp-com", external=True, source_provider="entra")

        resp = client.delete(f"{route}/jane-corp-com", headers=mgr)

        assert resp.status_code == 403, resp.text
        assert app.state.account_store.get("jane-corp-com") is not None

    def test_a_scim_refusal_is_a_scim_error(self, client, app, mgr):
        app.state.account_store.create("ops", _PASSWORD, groups=["dfe-admins"])
        app.state.group_store.add_member("dfe-admins", "ops")

        resp = client.delete(f"{SCIM_USERS}/ops", headers=mgr)

        assert resp.status_code == 403, resp.text
        assert resp.headers["content-type"].startswith("application/scim+json")
        assert resp.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]

    @_DELETE_ROUTES
    def test_an_account_in_groups_whose_roles_the_caller_holds_is_deleted(
        self, client, app, mgr, route
    ):
        app.state.account_store.create("helper", _PASSWORD, groups=["account-managers"])
        app.state.group_store.add_member("account-managers", "helper")

        resp = client.delete(f"{route}/helper", headers=mgr)

        assert resp.status_code == 204, resp.text
        assert "helper" not in app.state.group_store.get("account-managers").members

    def test_the_break_glass_floor_still_refuses_admin(
        self, recovery_accounts, client, admin_headers
    ):
        resp = client.delete(f"{ACCOUNTS}/{BREAKGLASS_USERNAME}", headers=admin_headers)

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "protected_account"
        assert recovery_accounts.state.account_store.get(BREAKGLASS_USERNAME) is not None
        assert (
            BREAKGLASS_USERNAME in recovery_accounts.state.group_store.get(BREAKGLASS_GROUP).members
        )


class TestADeletedAccountLeavesItsGroups:
    """Membership is keyed by name, so an account recreated under a deleted name must start empty."""

    @pytest.fixture
    def ghost(self, app) -> None:
        app.state.account_store.create("ghost", _PASSWORD, groups=["dfe-admins"])
        app.state.group_store.add_member("dfe-admins", "ghost")

    @pytest.mark.parametrize(
        "path",
        [f"{ACCOUNTS}/ghost", f"{SCIM_USERS}/ghost"],
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
