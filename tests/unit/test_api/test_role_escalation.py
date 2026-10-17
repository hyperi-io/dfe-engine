#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_role_escalation.py
#  Purpose:      Role and group writes never grant a permission the caller does not hold
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A caller holding role and group management alone cannot make itself admin.

mallory holds one custom role carrying ``role:*`` and ``group:*`` and nothing else,
the shape that used to add up to admin: a role write took any permission list, and
role:write let its holder hand any role to a group it belongs to.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.settings import DFESettings

ROLES = "/api/v1/auth/roles"
GROUPS = "/api/v1/auth/groups"
API_KEYS = "/api/v1/auth/api-keys"


def _bearer(api_settings: DFESettings, subject: str) -> dict[str, str]:
    token = create_access_token(data={"sub": subject}, settings=api_settings)
    return {"Authorization": f"Bearer {token}"}


def _create_role(client: TestClient, headers: dict[str, str], body: dict) -> None:
    resp = client.post(ROLES, json=body, headers=headers)
    assert resp.status_code == 201, resp.text


def _permissions(client: TestClient, headers: dict[str, str]) -> list[str]:
    resp = client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["permissions"]


@pytest.fixture
def mallory(
    app: FastAPI, client: TestClient, admin_headers: dict[str, str], api_settings: DFESettings
) -> dict[str, str]:
    """Headers for mallory, who holds role:*, group:* and api_key:* through one custom role."""
    _create_role(
        client,
        admin_headers,
        {"name": "role-manager", "permissions": ["role:*", "group:*", "api_key:*"]},
    )
    app.state.account_store.create("mallory", "pw-mallory-123456")
    resp = client.post(
        GROUPS,
        json={"name": "role-managers", "roles": ["role-manager"], "members": ["mallory"]},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return _bearer(api_settings, "mallory")


class TestARoleWriteHoldsToTheCallersPermissions:
    @pytest.mark.parametrize(
        "permissions",
        [["*"], ["query:execute"], ["config:*"], ["group:*", "account:write"]],
        ids=["wildcard", "unheld-action", "unheld-domain", "one-unheld-of-two"],
    )
    def test_a_role_granting_more_than_the_caller_holds_is_refused(
        self, app: FastAPI, client: TestClient, mallory: dict[str, str], permissions: list[str]
    ):
        body = {"name": "escalated", "permissions": permissions}
        resp = client.post(ROLES, json=body, headers=mallory)

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "forbidden"
        assert app.state.role_store.get("escalated") is None

    def test_a_role_within_the_callers_permissions_is_created(
        self, app: FastAPI, client: TestClient, mallory: dict[str, str]
    ):
        resp = client.post(
            ROLES, json={"name": "group-reader", "permissions": ["group:read"]}, headers=mallory
        )

        assert resp.status_code == 201, resp.text
        assert app.state.role_store.get("group-reader").permissions == ["group:read"]

    def test_an_update_widening_a_role_is_refused(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        mallory: dict[str, str],
    ):
        _create_role(client, admin_headers, {"name": "narrow", "permissions": ["group:read"]})

        resp = client.put(f"{ROLES}/narrow", json={"permissions": ["*"]}, headers=mallory)

        assert resp.status_code == 403, resp.text
        assert app.state.role_store.get("narrow").permissions == ["group:read"]

    def test_an_update_narrowing_a_role_the_caller_could_not_grant_is_refused(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        mallory: dict[str, str],
    ):
        """Every holder of the role loses what is taken away, so taking it needs holding it."""
        _create_role(
            client, admin_headers, {"name": "analyst-plus", "permissions": ["query:execute"]}
        )

        resp = client.put(f"{ROLES}/analyst-plus", json={"permissions": []}, headers=mallory)

        assert resp.status_code == 403, resp.text
        assert app.state.role_store.get("analyst-plus").permissions == ["query:execute"]

    def test_a_description_edit_needs_no_permissions(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        mallory: dict[str, str],
    ):
        _create_role(client, admin_headers, {"name": "wide", "permissions": ["*"]})

        resp = client.put(f"{ROLES}/wide", json={"description": "Renamed"}, headers=mallory)

        assert resp.status_code == 200, resp.text
        assert app.state.role_store.get("wide").description == "Renamed"

    def test_admin_still_creates_any_role(
        self, app: FastAPI, client: TestClient, admin_headers: dict[str, str]
    ):
        _create_role(client, admin_headers, {"name": "everything", "permissions": ["*"]})

        assert app.state.role_store.get("everything").permissions == ["*"]


class TestRoleWriteDoesNotHandOutRoles:
    def test_the_admin_role_cannot_be_added_to_the_callers_own_group(
        self, app: FastAPI, client: TestClient, mallory: dict[str, str]
    ):
        resp = client.put(
            f"{GROUPS}/role-managers", json={"roles": ["role-manager", "admin"]}, headers=mallory
        )

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("role-managers").roles == ["role-manager"]
        assert "*" not in _permissions(client, mallory)

    def test_a_new_group_carrying_admin_with_the_caller_in_it_is_refused(
        self, app: FastAPI, client: TestClient, mallory: dict[str, str]
    ):
        resp = client.post(
            GROUPS,
            json={"name": "back-door", "roles": ["admin"], "members": ["mallory"]},
            headers=mallory,
        )

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("back-door") is None
        assert "*" not in _permissions(client, mallory)

    def test_the_caller_cannot_join_the_admin_group(
        self, app: FastAPI, client: TestClient, mallory: dict[str, str]
    ):
        body = {"username": "mallory"}
        resp = client.post(f"{GROUPS}/dfe-admins/members", json=body, headers=mallory)

        assert resp.status_code == 403, resp.text
        assert "mallory" not in app.state.group_store.get("dfe-admins").members

    def test_an_api_key_bound_to_the_admin_group_is_refused(
        self, app: FastAPI, client: TestClient, mallory: dict[str, str]
    ):
        body = {"name": "skeleton", "groups": ["dfe-admins"]}
        resp = client.post(API_KEYS, json=body, headers=mallory)

        assert resp.status_code == 403, resp.text
        assert app.state.api_key_store.get("skeleton") is None

    def test_a_role_the_caller_wrote_within_its_own_permissions_can_be_handed_out(
        self, app: FastAPI, client: TestClient, mallory: dict[str, str]
    ):
        _create_role(client, mallory, {"name": "group-reader", "permissions": ["group:read"]})

        resp = client.post(
            GROUPS, json={"name": "readers", "roles": ["group-reader"]}, headers=mallory
        )

        assert resp.status_code == 201, resp.text


class TestAScopedRoleDoesNotBecomeAPlatformOne:
    """A scoped role is held to its holder's orgs, and the same permissions unscoped read every org."""

    @pytest.fixture
    def tenant_manager(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        api_settings: DFESettings,
    ) -> dict[str, str]:
        _create_role(
            client,
            admin_headers,
            {
                "name": "tenant-manager",
                "permissions": ["role:*", "group:*", "query:execute"],
                "scoped": True,
            },
        )
        app.state.account_store.create("trent", "pw-trent-12345678")
        resp = client.post(
            GROUPS,
            json={"name": "tenant-managers", "roles": ["tenant-manager"], "members": ["trent"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        return _bearer(api_settings, "trent")

    def test_an_unscoped_role_from_scoped_permissions_is_refused(
        self, app: FastAPI, client: TestClient, tenant_manager: dict[str, str]
    ):
        resp = client.post(
            ROLES,
            json={"name": "reads-everything", "permissions": ["query:execute"], "scoped": False},
            headers=tenant_manager,
        )

        assert resp.status_code == 403, resp.text
        assert app.state.role_store.get("reads-everything") is None

    def test_unscoping_a_scoped_role_is_refused(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        tenant_manager: dict[str, str],
    ):
        _create_role(
            client,
            admin_headers,
            {"name": "tenant-query", "permissions": ["query:execute"], "scoped": True},
        )

        resp = client.put(f"{ROLES}/tenant-query", json={"scoped": False}, headers=tenant_manager)

        assert resp.status_code == 403, resp.text
        assert app.state.role_store.get("tenant-query").scoped is True

    def test_a_scoped_role_from_scoped_permissions_is_created(
        self, app: FastAPI, client: TestClient, tenant_manager: dict[str, str]
    ):
        resp = client.post(
            ROLES,
            json={"name": "tenant-reader", "permissions": ["query:execute"], "scoped": True},
            headers=tenant_manager,
        )

        assert resp.status_code == 201, resp.text

    def test_a_platform_role_cannot_be_handed_out_system_wide(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        tenant_manager: dict[str, str],
    ):
        """The caller holds query:execute, but only through a scoped role."""
        _create_role(
            client, admin_headers, {"name": "plain-query", "permissions": ["query:execute"]}
        )

        resp = client.post(
            GROUPS, json={"name": "readers", "roles": ["plain-query"]}, headers=tenant_manager
        )

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("readers") is None

    def test_a_scoped_role_can_be_handed_out_system_wide(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        tenant_manager: dict[str, str],
    ):
        _create_role(
            client,
            admin_headers,
            {"name": "tenant-query", "permissions": ["query:execute"], "scoped": True},
        )

        resp = client.post(
            GROUPS, json={"name": "readers", "roles": ["tenant-query"]}, headers=tenant_manager
        )

        assert resp.status_code == 201, resp.text
