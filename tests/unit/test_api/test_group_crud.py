#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_group_crud.py
#  Purpose:      Tests for group CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for POST/GET/PUT/DELETE /api/v1/auth/groups endpoints."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from tests.unit.test_api.group_crud_cases import (
    GROUP_DELETE_ROUTE_CASES,
    KNOWN_SOURCE_PROVIDER_CASES,
    LINK_CHANGE_CASES,
    MALFORMED_SOURCE_ID_CASES,
    MISSING_PROVIDER_CASES,
    UNLOADABLE_PROVIDER_FILE_CASES,
    GroupDeleteRouteCase,
    KnownSourceProviderCase,
    LinkChangeCase,
    MalformedSourceIdCase,
    MissingProviderCase,
    UnloadableProviderFileCase,
)
from tests.unit.test_auth.factories import make_oidc_provider


class TestCreateGroup:
    """POST /api/v1/auth/groups"""

    def test_create_group(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/groups",
            json={"name": "test-group", "roles": ["data_viewer"], "description": "A test group"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-group"
        assert data["roles"] == ["data_viewer"]
        assert data["description"] == "A test group"
        assert data["members"] == []

    def test_create_group_with_members(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/groups",
            json={
                "name": "members-on-create",
                "roles": ["data_viewer"],
                "members": ["admin", "admin"],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["members"] == ["admin"]

    def test_create_duplicate_returns_409(self, client, admin_headers):
        client.post(
            "/api/v1/auth/groups",
            json={"name": "dup-group", "roles": ["admin"]},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/auth/groups",
            json={"name": "dup-group", "roles": ["admin"]},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_create_requires_admin(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/auth/groups",
            json={"name": "blocked", "roles": ["admin"]},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_create_missing_fields_returns_422(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/groups",
            json={},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_a_link_is_stored_and_returned(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        body = {
            "name": "okta-soc",
            "roles": ["data_viewer"],
            "source_id": "SOC Analysts",
            "source_provider": "oidc",
        }

        created = client.post("/api/v1/auth/groups", headers=admin_headers, json=body)
        fetched = client.get("/api/v1/auth/groups/okta-soc", headers=admin_headers)

        assert created.status_code == 201, created.text
        stored = app.state.group_store.get("okta-soc")
        assert (stored.source_id, stored.source_provider) == ("SOC Analysts", "oidc")
        link = {"source_id": "SOC Analysts", "source_provider": "oidc"}
        assert {key: created.json()[key] for key in link} == link
        assert {key: fetched.json()[key] for key in link} == link

    def test_an_unlinked_group_reads_back_empty_link_fields(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        created = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={"name": "local-only", "roles": ["data_viewer"]},
        )

        assert created.status_code == 201, created.text
        assert (created.json()["source_id"], created.json()["source_provider"]) == ("", "")


class TestGroupRoleEscalation:
    """A group:write caller must not grant a group a role it does not itself
    hold (else it could self-escalate by joining the group). role:write, or
    already holding the role, is required."""

    def test_create_cannot_assign_unheld_role(self, client, operator_headers):
        # operator has group:write (infra_admin) but not role:write, and does
        # not hold `admin` - it must not mint a group carrying admin.
        resp = client.post(
            "/api/v1/auth/groups",
            json={"name": "escalate-attempt", "roles": ["admin"]},
            headers=operator_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"

    def test_create_can_assign_held_role(self, client, operator_headers):
        # A role the caller DOES hold (infra_admin) is allowed.
        resp = client.post(
            "/api/v1/auth/groups",
            json={"name": "held-role-ok", "roles": ["infra_admin"]},
            headers=operator_headers,
        )
        assert resp.status_code == 201

    def test_admin_can_assign_any_role(self, client, admin_headers):
        # admin holds role:write (via "*") - no restriction.
        resp = client.post(
            "/api/v1/auth/groups",
            json={"name": "admin-assigns-admin", "roles": ["admin"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201

    def test_update_cannot_escalate_roles(self, client, admin_headers, operator_headers):
        # admin makes a plain group operator can manage...
        client.post(
            "/api/v1/auth/groups",
            json={"name": "upd-escalate", "roles": ["infra_admin"]},
            headers=admin_headers,
        )
        # ...operator (no role:write, no admin) cannot PUT admin onto it.
        resp = client.put(
            "/api/v1/auth/groups/upd-escalate",
            json={"roles": ["admin"]},
            headers=operator_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"


def _roles(client, headers) -> list[str]:
    resp = client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["roles"]


class TestJoiningAGroupNeedsItsRoles:
    """A new member takes every role its group carries, so adding one is assigning them.

    operator holds group:* through infra_admin, and neither admin nor role:write.
    """

    @pytest.mark.parametrize("username", ["operator", "viewer"])
    def test_a_member_cannot_be_added_to_the_admin_group(
        self, client, app, operator_headers, username
    ):
        resp = client.post(
            "/api/v1/auth/groups/dfe-admins/members",
            json={"username": username},
            headers=operator_headers,
        )

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "forbidden"
        assert username not in app.state.group_store.get("dfe-admins").members
        assert "admin" not in _roles(client, operator_headers)

    def test_a_member_cannot_be_put_into_the_admin_group(self, client, app, operator_headers):
        members = [*app.state.group_store.get("dfe-admins").members, "operator"]

        resp = client.put(
            "/api/v1/auth/groups/dfe-admins", json={"members": members}, headers=operator_headers
        )

        assert resp.status_code == 403, resp.text
        assert "operator" not in app.state.group_store.get("dfe-admins").members
        assert "admin" not in _roles(client, operator_headers)

    def test_admin_still_adds_a_member(self, client, app, admin_headers):
        resp = client.post(
            "/api/v1/auth/groups/dfe-admins/members",
            json={"username": "operator"},
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert "operator" in app.state.group_store.get("dfe-admins").members

    def test_a_group_whose_roles_the_caller_holds_takes_a_new_member(
        self, client, app, operator_headers
    ):
        """dfe-infra carries infra_admin, which operator holds."""
        by_post = client.post(
            "/api/v1/auth/groups/dfe-infra/members",
            json={"username": "viewer"},
            headers=operator_headers,
        )
        members = [*app.state.group_store.get("dfe-infra").members, "admin"]
        by_put = client.put(
            "/api/v1/auth/groups/dfe-infra", json={"members": members}, headers=operator_headers
        )

        assert by_post.status_code == 200, by_post.text
        assert by_put.status_code == 200, by_put.text
        assert {"viewer", "admin"} <= set(app.state.group_store.get("dfe-infra").members)


class TestLeavingAGroupNeedsItsRolesToo:
    """A removed member loses every role its group carries, so removing one is taking them away.

    operator holds group:* through infra_admin, and neither admin nor role:write.
    """

    @pytest.fixture
    def viewer_is_admin(self, app) -> None:
        app.state.group_store.add_member("dfe-admins", "viewer")

    def test_a_member_cannot_be_removed_from_the_admin_group(
        self, client, app, operator_headers, viewer_is_admin
    ):
        resp = client.delete(
            "/api/v1/auth/groups/dfe-admins/members/viewer", headers=operator_headers
        )

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "forbidden"
        assert "viewer" in app.state.group_store.get("dfe-admins").members

    def test_a_member_cannot_be_put_out_of_the_admin_group(
        self, client, app, operator_headers, viewer_is_admin
    ):
        members = [m for m in app.state.group_store.get("dfe-admins").members if m != "viewer"]

        resp = client.put(
            "/api/v1/auth/groups/dfe-admins", json={"members": members}, headers=operator_headers
        )

        assert resp.status_code == 403, resp.text
        assert "viewer" in app.state.group_store.get("dfe-admins").members

    def test_admin_still_removes_a_member(self, client, app, admin_headers, viewer_is_admin):
        resp = client.delete("/api/v1/auth/groups/dfe-admins/members/viewer", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert "viewer" not in app.state.group_store.get("dfe-admins").members

    def test_a_group_whose_roles_the_caller_holds_loses_a_member(
        self, client, app, operator_headers
    ):
        """dfe-infra carries infra_admin, which operator holds."""
        app.state.group_store.add_member("dfe-infra", "viewer")

        resp = client.delete(
            "/api/v1/auth/groups/dfe-infra/members/viewer", headers=operator_headers
        )

        assert resp.status_code == 200, resp.text
        assert "viewer" not in app.state.group_store.get("dfe-infra").members


class TestTakingARoleOffAGroupNeedsIt:
    """A role taken off a group is taken from every member, the recovery credentials included."""

    def test_admin_cannot_be_taken_off_the_admin_group(self, client, app, operator_headers):
        app.state.group_store.update("dfe-admins", source_id="dfe-admins")
        resp = client.put(
            "/api/v1/auth/groups/dfe-admins",
            json={"roles": ["infra_admin"]},
            headers=operator_headers,
        )

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("dfe-admins").roles == ["admin"]
        assert "admin" in _roles(
            client, {"X-Oidc-Subject": "ann@corp", "X-Oidc-Groups": "dfe-admins"}
        )

    def test_admin_cannot_take_admin_off_the_admin_group_either(
        self, recovery_accounts, client, admin_headers
    ):
        """The recovery floor: breakglass and the local admin hold admin through this group."""
        resp = client.put(
            "/api/v1/auth/groups/dfe-admins",
            json={"roles": ["infra_admin"]},
            headers=admin_headers,
        )

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "protected_account"
        assert recovery_accounts.state.group_store.get("dfe-admins").roles == ["admin"]

    def test_a_role_the_caller_does_not_hold_cannot_be_removed(self, client, app, operator_headers):
        app.state.group_store.create("sec-leads", ["admin", "infra_admin"], members=["viewer"])

        resp = client.put(
            "/api/v1/auth/groups/sec-leads",
            json={"roles": ["infra_admin"]},
            headers=operator_headers,
        )

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("sec-leads").roles == ["admin", "infra_admin"]

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"roles": ["infra_admin"], "members": ["viewer"]}, id="role-dropped"),
            pytest.param({"roles": ["admin", "infra_admin"], "members": []}, id="member-dropped"),
            pytest.param(
                {"roles": ["admin", "infra_admin"], "members": ["viewer", "operator"]},
                id="member-added",
            ),
        ],
    )
    def test_a_combined_roles_and_members_update_is_refused(
        self, client, app, operator_headers, body
    ):
        app.state.group_store.create("sec-leads", ["admin", "infra_admin"], members=["viewer"])

        resp = client.put("/api/v1/auth/groups/sec-leads", json=body, headers=operator_headers)

        assert resp.status_code == 403, resp.text
        stored = app.state.group_store.get("sec-leads")
        assert (stored.roles, stored.members) == (["admin", "infra_admin"], ["viewer"])

    def test_a_combined_update_within_the_callers_roles_is_accepted(
        self, client, app, operator_headers
    ):
        app.state.group_store.create("infra-leads", ["infra_admin"], members=["viewer"])

        resp = client.put(
            "/api/v1/auth/groups/infra-leads",
            json={"roles": ["infra_admin", "data_analyst"], "members": ["operator"]},
            headers=operator_headers,
        )

        assert resp.status_code == 200, resp.text
        stored = app.state.group_store.get("infra-leads")
        assert (stored.roles, stored.members) == (["infra_admin", "data_analyst"], ["operator"])


class TestJoiningAnOrgGroupNeedsItsRolesInThatOrg:
    """The same rule at org scope: group:* in acme does not make its holder acme's admin."""

    @pytest.fixture
    def acme(self, client, app, admin_headers, api_settings) -> dict[str, dict[str, str]]:
        """acme-admins (admin) and acme-infra (infra_admin), each with one member."""
        resp = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        for username, group, role in (
            ("orgadmin", "acme-admins", "admin"),
            ("orgop", "acme-infra", "infra_admin"),
        ):
            app.state.account_store.create(username, secrets.token_urlsafe(16))
            app.state.group_store.create(group, [role], members=[username], scope="org:acme")
        return {
            username: {
                "Authorization": "Bearer "
                + create_access_token(data={"sub": username}, settings=api_settings)
            }
            for username in ("orgadmin", "orgop")
        }

    def test_an_org_infra_admin_cannot_join_the_org_admins(self, client, app, acme):
        by_post = client.post(
            "/api/v1/auth/groups/acme-admins/members",
            json={"username": "orgop"},
            headers=acme["orgop"],
        )
        by_put = client.put(
            "/api/v1/auth/groups/acme-admins",
            json={"members": ["orgadmin", "orgop"]},
            headers=acme["orgop"],
        )

        assert by_post.status_code == 403, by_post.text
        assert by_put.status_code == 403, by_put.text
        assert app.state.group_store.get("acme-admins").members == ["orgadmin"]

    def test_an_admin_of_another_org_cannot_join_the_org_admins(
        self, client, app, admin_headers, api_settings, acme
    ):
        """crosser is globex's admin and acme's infra_admin: admin in globex is not admin in acme."""
        resp = client.post("/api/v1/orgs", json={"name": "globex"}, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        app.state.account_store.create("crosser", secrets.token_urlsafe(16))
        app.state.group_store.create(
            "globex-admins", ["admin"], members=["crosser"], scope="org:globex"
        )
        app.state.group_store.add_member("acme-infra", "crosser")
        headers = {
            "Authorization": "Bearer "
            + create_access_token(data={"sub": "crosser"}, settings=api_settings)
        }

        resp = client.post(
            "/api/v1/auth/groups/acme-admins/members",
            json={"username": "crosser"},
            headers=headers,
        )

        assert resp.status_code == 403, resp.text
        assert "crosser" not in app.state.group_store.get("acme-admins").members
        created = client.post(
            "/api/v1/auth/groups",
            json={"name": "acme-own", "roles": ["admin"], "scope": "org:acme"},
            headers=headers,
        )
        assert created.status_code == 403, created.text

    def test_the_org_admin_still_adds_a_member(self, client, app, acme):
        resp = client.post(
            "/api/v1/auth/groups/acme-admins/members",
            json={"username": "orgop"},
            headers=acme["orgadmin"],
        )

        assert resp.status_code == 200, resp.text
        assert "orgop" in app.state.group_store.get("acme-admins").members

    def test_an_org_infra_admin_cannot_remove_the_org_admin(self, client, app, acme):
        resp = client.delete(
            "/api/v1/auth/groups/acme-admins/members/orgadmin", headers=acme["orgop"]
        )

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("acme-admins").members == ["orgadmin"]

    def test_the_org_admin_still_removes_a_member(self, client, app, acme):
        app.state.group_store.add_member("acme-admins", "orgop")

        resp = client.delete(
            "/api/v1/auth/groups/acme-admins/members/orgop", headers=acme["orgadmin"]
        )

        assert resp.status_code == 200, resp.text
        assert app.state.group_store.get("acme-admins").members == ["orgadmin"]


class TestScimJoinsNeedTheGroupsRoles:
    """SCIM group writes need group:write, which operator holds, so the same rule applies."""

    SCIM = "/api/v1/scim/v2/Groups"
    PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
    GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"

    def _patch_add(self, username: str) -> dict:
        return {
            "schemas": [self.PATCH_SCHEMA],
            "Operations": [{"op": "add", "path": "members", "value": [{"value": username}]}],
        }

    def test_a_patch_cannot_add_a_member_to_the_admin_group(self, client, app, operator_headers):
        resp = client.patch(
            f"{self.SCIM}/dfe-admins", json=self._patch_add("operator"), headers=operator_headers
        )

        assert resp.status_code == 403, resp.text
        assert "operator" not in app.state.group_store.get("dfe-admins").members

    def test_a_put_cannot_add_a_member_to_the_admin_group(self, client, app, operator_headers):
        members = [*app.state.group_store.get("dfe-admins").members, "operator"]
        body = {
            "schemas": [self.GROUP_SCHEMA],
            "displayName": "dfe-admins",
            "members": [{"value": username} for username in members],
        }

        resp = client.put(f"{self.SCIM}/dfe-admins", json=body, headers=operator_headers)

        assert resp.status_code == 403, resp.text
        assert "operator" not in app.state.group_store.get("dfe-admins").members

    def test_admin_still_adds_a_member_through_scim(self, client, app, admin_headers):
        resp = client.patch(
            f"{self.SCIM}/dfe-admins", json=self._patch_add("operator"), headers=admin_headers
        )

        assert resp.status_code == 200, resp.text
        assert "operator" in app.state.group_store.get("dfe-admins").members

    def test_a_refusal_is_a_scim_error(self, client, operator_headers):
        resp = client.patch(
            f"{self.SCIM}/dfe-admins", json=self._patch_add("operator"), headers=operator_headers
        )

        assert resp.status_code == 403, resp.text
        assert resp.headers["content-type"].startswith("application/scim+json")
        assert resp.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        assert resp.json()["status"] == "403"


def _scim_admins_body(members: list[str], external_id: str | None = None) -> dict:
    """A SCIM PUT body for dfe-admins with *members*, and *external_id* when given."""
    body = {
        "schemas": [TestScimJoinsNeedTheGroupsRoles.GROUP_SCHEMA],
        "displayName": "dfe-admins",
        "members": [{"value": username} for username in members],
    }
    if external_id is not None:
        body["externalId"] = external_id
    return body


class TestScimLeavesAndProviderIdsNeedTheGroupsRoles:
    """Removing a member, or moving the provider id an IdP login resolves, changes who holds a role."""

    SCIM = TestScimJoinsNeedTheGroupsRoles.SCIM
    PATCH_SCHEMA = TestScimJoinsNeedTheGroupsRoles.PATCH_SCHEMA

    @pytest.fixture
    def viewer_is_admin(self, app) -> list[str]:
        app.state.group_store.add_member("dfe-admins", "viewer")
        return app.state.group_store.get("dfe-admins").members

    @pytest.mark.parametrize(
        "operation",
        [
            pytest.param({"op": "remove", "path": 'members[value eq "viewer"]'}, id="filter"),
            pytest.param(
                {"op": "remove", "path": "members", "value": [{"value": "viewer"}]}, id="value"
            ),
        ],
    )
    def test_a_patch_cannot_remove_a_member_of_the_admin_group(
        self, client, app, operator_headers, viewer_is_admin, operation
    ):
        body = {"schemas": [self.PATCH_SCHEMA], "Operations": [operation]}

        resp = client.patch(f"{self.SCIM}/dfe-admins", json=body, headers=operator_headers)

        assert resp.status_code == 403, resp.text
        assert "viewer" in app.state.group_store.get("dfe-admins").members

    def test_a_put_cannot_drop_a_member_of_the_admin_group(
        self, client, app, operator_headers, viewer_is_admin
    ):
        members = [m for m in viewer_is_admin if m != "viewer"]

        resp = client.put(
            f"{self.SCIM}/dfe-admins", json=_scim_admins_body(members), headers=operator_headers
        )

        assert resp.status_code == 403, resp.text
        assert "viewer" in app.state.group_store.get("dfe-admins").members

    def test_a_delete_cannot_drop_the_members_of_an_admin_group(
        self, client, app, operator_headers
    ):
        app.state.group_store.create("sec-leads", ["admin"], members=["viewer"])

        resp = client.delete(f"{self.SCIM}/sec-leads", headers=operator_headers)

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("sec-leads").members == ["viewer"]

    @pytest.mark.parametrize("current", ["", "entra-admins-guid"], ids=["newly-set", "changed"])
    def test_a_put_cannot_move_the_admin_groups_provider_id(
        self, client, app, operator_headers, current
    ):
        """An IdP login naming the new id would resolve to dfe-admins, and to admin."""
        app.state.group_store.update("dfe-admins", source_id=current)
        members = app.state.group_store.get("dfe-admins").members

        resp = client.put(
            f"{self.SCIM}/dfe-admins",
            json=_scim_admins_body(members, external_id="mallory-own-guid"),
            headers=operator_headers,
        )
        idp = {"X-Oidc-Subject": "mallory@corp", "X-Oidc-Groups": "mallory-own-guid"}

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("dfe-admins").source_id == current
        assert "admin" not in _roles(client, idp)

    def test_admin_still_moves_a_provider_id(self, client, app, admin_headers):
        members = app.state.group_store.get("dfe-admins").members

        resp = client.put(
            f"{self.SCIM}/dfe-admins",
            json=_scim_admins_body(members, external_id="entra-admins-guid"),
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert app.state.group_store.get("dfe-admins").source_id == "entra-admins-guid"

    def test_admin_still_removes_a_member_through_scim(
        self, client, app, admin_headers, viewer_is_admin
    ):
        body = {
            "schemas": [self.PATCH_SCHEMA],
            "Operations": [{"op": "remove", "path": 'members[value eq "viewer"]'}],
        }

        resp = client.patch(f"{self.SCIM}/dfe-admins", json=body, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert "viewer" not in app.state.group_store.get("dfe-admins").members


def _scim_group_body(name: str, external_id: str) -> dict:
    """A SCIM Group body for a memberless group *name* carrying *external_id*."""
    return {
        "schemas": [TestScimJoinsNeedTheGroupsRoles.GROUP_SCHEMA],
        "displayName": name,
        "externalId": external_id,
        "members": [],
    }


class TestAProviderIdNamesOneGroup:
    """Login resolves a provider id to one group, the last by name, so no second group may take it.

    ``zz-shadow`` sorts after ``dfe-admins``, so on a shared id it is the group login picks.
    """

    SCIM = TestScimJoinsNeedTheGroupsRoles.SCIM
    IDP = {"X-Oidc-Subject": "jane@corp", "X-Oidc-Groups": "entra-admins-guid"}

    @pytest.fixture(autouse=True)
    def admins_carry_a_provider_id(self, app, client) -> None:
        """Depends on ``client``, whose lifespan builds the stores."""
        app.state.group_store.update("dfe-admins", source_id="entra-admins-guid")

    @pytest.mark.parametrize("caller", ["operator_headers", "admin_headers"])
    def test_a_post_cannot_take_another_groups_provider_id(self, client, app, request, caller):
        resp = client.post(
            self.SCIM,
            json=_scim_group_body("zz-shadow", "entra-admins-guid"),
            headers=request.getfixturevalue(caller),
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["scimType"] == "uniqueness"
        assert app.state.group_store.get("zz-shadow") is None
        assert "admin" in _roles(client, self.IDP)

    def test_a_put_cannot_take_another_groups_provider_id(self, client, app, operator_headers):
        """A group with no roles passes the role check, so the id itself has to be refused."""
        app.state.group_store.create("zz-shadow", [], members=[])

        resp = client.put(
            f"{self.SCIM}/zz-shadow",
            json=_scim_group_body("zz-shadow", "entra-admins-guid"),
            headers=operator_headers,
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["scimType"] == "uniqueness"
        assert app.state.group_store.get("zz-shadow").source_id == ""
        assert "admin" in _roles(client, self.IDP)

    def test_a_new_provider_id_is_still_provisioned(self, client, app, operator_headers):
        resp = client.post(
            self.SCIM, json=_scim_group_body("sec-team", "entra-sec-guid"), headers=operator_headers
        )

        assert resp.status_code == 201, resp.text
        assert app.state.group_store.get("sec-team").source_id == "entra-sec-guid"

    def test_a_group_resending_its_own_provider_id_is_accepted(self, client, app, operator_headers):
        app.state.group_store.create("sec-team", [], members=[])
        app.state.group_store.update("sec-team", source_id="entra-sec-guid")

        resp = client.put(
            f"{self.SCIM}/sec-team",
            json=_scim_group_body("sec-team", "entra-sec-guid"),
            headers=operator_headers,
        )

        assert resp.status_code == 200, resp.text


class TestAGroupsApiSourceIdNamesOneGroup:
    """The groups API refuses a source ID another group carries, by the predicate SCIM uses."""

    IDP = {"X-Oidc-Subject": "jane@corp", "X-Oidc-Groups": "dfe-admins"}

    @pytest.fixture(autouse=True)
    def admins_carry_a_source_id(self, app: FastAPI, client: TestClient) -> None:
        """Link dfe-admins to the name an IdP sends for it; depends on client, whose lifespan builds the stores."""
        app.state.group_store.update(name="dfe-admins", source_id="dfe-admins")

    def test_a_create_cannot_take_another_groups_source_id(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "zz-shadow",
                "roles": [],
                "source_id": "dfe-admins",
                "source_provider": "oidc",
            },
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict"
        assert app.state.group_store.get("zz-shadow") is None
        assert "admin" in _roles(client=client, headers=self.IDP)

    def test_an_update_cannot_take_another_groups_source_id(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"source_id": "dfe-admins", "source_provider": "oidc"},
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict"
        assert app.state.group_store.get("dfe-viewers").source_id == ""
        assert "admin" in _roles(client=client, headers=self.IDP)

    def test_a_group_resending_its_own_source_id_is_accepted(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        resp = client.put(
            "/api/v1/auth/groups/dfe-admins",
            headers=admin_headers,
            json={"description": "Administrators", "source_id": "dfe-admins"},
        )

        assert resp.status_code == 200, resp.text

    def test_scim_cannot_take_a_source_id_the_groups_api_set(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        linked = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"source_id": "00g-viewers", "source_provider": "oidc"},
        )

        resp = client.post(
            TestScimJoinsNeedTheGroupsRoles.SCIM,
            headers=admin_headers,
            json=_scim_group_body(external_id="00g-viewers", name="zz-shadow"),
        )

        assert linked.status_code == 200, linked.text
        assert resp.status_code == 409, resp.text
        assert resp.json()["scimType"] == "uniqueness"
        assert app.state.group_store.get("zz-shadow") is None


class TestMovingALinkNeedsTheGroupsRoles:
    """A login asserting a group's source ID takes its roles, so moving the link hands them out or takes them away.

    operator holds group:* through infra_admin but neither admin nor role:write.
    """

    @pytest.mark.parametrize(
        "case", LINK_CHANGE_CASES, ids=[case["id"] for case in LINK_CHANGE_CASES]
    )
    def test_a_link_change_on_the_admin_group_is_refused(
        self,
        app: FastAPI,
        client: TestClient,
        operator_headers: dict[str, str],
        case: LinkChangeCase,
    ):
        app.state.group_store.update(name="dfe-admins", **case["current"])

        resp = client.put(
            "/api/v1/auth/groups/dfe-admins", headers=operator_headers, json=case["change"]
        )

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "forbidden"
        stored = app.state.group_store.get("dfe-admins")
        link = {"source_id": stored.source_id, "source_provider": stored.source_provider}
        assert link == case["current"]

    def test_a_group_whose_roles_the_caller_holds_takes_a_link(
        self, app: FastAPI, client: TestClient, operator_headers: dict[str, str]
    ):
        resp = client.put(
            "/api/v1/auth/groups/dfe-infra",
            headers=operator_headers,
            json={"source_id": "dfe-infra", "source_provider": "oidc"},
        )

        assert resp.status_code == 200, resp.text
        stored = app.state.group_store.get("dfe-infra")
        assert (stored.source_id, stored.source_provider) == ("dfe-infra", "oidc")

    def test_an_edit_resending_the_link_unchanged_needs_no_roles(
        self, app: FastAPI, client: TestClient, operator_headers: dict[str, str]
    ):
        app.state.group_store.update(
            name="dfe-admins", source_id="dfe-admins", source_provider="deleted-idp"
        )

        resp = client.put(
            "/api/v1/auth/groups/dfe-admins",
            headers=operator_headers,
            json={
                "description": "Administrators",
                "source_id": "dfe-admins",
                "source_provider": "deleted-idp",
            },
        )

        assert resp.status_code == 200, resp.text
        assert app.state.group_store.get("dfe-admins").description == "Administrators"

    def test_an_unauthorised_link_to_a_taken_id_is_forbidden_not_a_conflict(
        self, app: FastAPI, client: TestClient, operator_headers: dict[str, str]
    ):
        app.state.group_store.update(
            name="dfe-analysts", source_id="taken-id", source_provider="oidc"
        )

        resp = client.put(
            "/api/v1/auth/groups/dfe-admins",
            headers=operator_headers,
            json={"source_id": "taken-id", "source_provider": "oidc"},
        )

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "forbidden"

    def test_a_refused_link_change_is_audited_as_denied(
        self, audit_events: list[dict], client: TestClient, operator_headers: dict[str, str]
    ):
        resp = client.put(
            "/api/v1/auth/groups/dfe-admins",
            headers=operator_headers,
            json={"source_id": "mallory-group", "source_provider": "oidc"},
        )

        assert resp.status_code == 403, resp.text
        denied = [event for event in audit_events if event["event"] == "auth.permission.denied"]
        assert [(event["user_id"], event["action"]) for event in denied] == [
            ("operator", "role:write")
        ]


def _write_provider_file(*, app: FastAPI, content: bytes, name: str) -> None:
    """Write *content* as provider *name*'s file in the app's OIDC provider registry directory."""
    providers_dir = Path(app.state.settings.auth.auth_dir) / "oidc-providers"
    (providers_dir / f"{name}.yaml").write_bytes(content)


class TestALinksProviderIsOneAnAssertionCarries:
    """A provider no login can assert links a group for nobody, so a changed provider must be a known name."""

    @pytest.fixture
    def known_providers(self, app: FastAPI, client: TestClient) -> None:
        """Register the okta OIDC provider and bind the okta-scim stamp to entra."""
        app.state.oidc_provider_registry.create(name="okta", provider=make_oidc_provider())
        app.state.settings.auth.source_provider_bindings = {"okta-scim": "entra"}

    @pytest.mark.parametrize(
        "case",
        KNOWN_SOURCE_PROVIDER_CASES,
        ids=[case["id"] for case in KNOWN_SOURCE_PROVIDER_CASES],
    )
    def test_a_known_name_is_accepted(
        self,
        admin_headers: dict[str, str],
        app: FastAPI,
        client: TestClient,
        known_providers: None,
        case: KnownSourceProviderCase,
    ):
        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "linked",
                "roles": [],
                "source_id": "SOC Analysts",
                "source_provider": case["source_provider"],
            },
        )

        assert resp.status_code == 201, resp.text
        assert app.state.group_store.get("linked").source_provider == case["source_provider"]

    def test_an_unknown_name_is_refused_on_create(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "linked",
                "roles": [],
                "source_id": "SOC Analysts",
                "source_provider": "okta-typo",
            },
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_source_provider"
        assert app.state.group_store.get("linked") is None

    def test_an_unknown_name_is_refused_on_update(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"source_id": "dfe-viewers", "source_provider": "okta-typo"},
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_source_provider"
        stored = app.state.group_store.get("dfe-viewers")
        assert (stored.source_id, stored.source_provider) == ("", "")

    @pytest.mark.parametrize(
        "case",
        UNLOADABLE_PROVIDER_FILE_CASES,
        ids=[case["id"] for case in UNLOADABLE_PROVIDER_FILE_CASES],
    )
    def test_a_provider_whose_file_does_not_load_is_refused_on_create(
        self,
        admin_headers: dict[str, str],
        app: FastAPI,
        client: TestClient,
        case: UnloadableProviderFileCase,
    ):
        _write_provider_file(app=app, content=case["content"], name="broken-idp")

        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "linked",
                "roles": [],
                "source_id": "SOC Analysts",
                "source_provider": "broken-idp",
            },
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_source_provider"
        assert app.state.group_store.get("linked") is None

    def test_a_provider_whose_file_does_not_load_is_refused_on_update(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        _write_provider_file(app=app, content=b"type: [generic\n", name="broken-idp")

        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"source_id": "dfe-viewers", "source_provider": "broken-idp"},
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_source_provider"
        stored = app.state.group_store.get("dfe-viewers")
        assert (stored.source_id, stored.source_provider) == ("", "")

    def test_a_stale_name_left_unchanged_is_accepted(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        app.state.group_store.update(
            name="dfe-viewers", source_id="00g-viewers", source_provider="deleted-idp"
        )

        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"roles": ["data_viewer"], "source_provider": "deleted-idp"},
        )

        assert resp.status_code == 200, resp.text
        assert app.state.group_store.get("dfe-viewers").roles == ["data_viewer"]

    def test_a_scim_group_with_no_source_id_takes_a_roles_edit(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        app.state.group_store.create(members=[], name="scim-made", roles=[])
        app.state.group_store.update(name="scim-made", source_id="", source_provider="scim")

        resp = client.put(
            "/api/v1/auth/groups/scim-made",
            headers=admin_headers,
            json={"roles": ["data_viewer"], "source_id": "", "source_provider": "scim"},
        )

        assert resp.status_code == 200, resp.text
        assert app.state.group_store.get("scim-made").roles == ["data_viewer"]

    def test_the_proxy_provider_is_unknown_while_the_proxy_headers_are_untrusted(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        app.state.settings.auth.trust_proxy_auth_headers = False

        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "linked",
                "roles": [],
                "source_id": "SOC Analysts",
                "source_provider": "oidc",
            },
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_source_provider"

    def test_an_overlong_name_is_refused_without_echoing_it(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        provider = "p" * 513

        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "linked",
                "roles": [],
                "source_id": "SOC Analysts",
                "source_provider": provider,
            },
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_source_provider"
        assert provider not in resp.json()["message"]


class TestASourceIdIsMatchedExactly:
    """Login compares a source ID verbatim, so one with surrounding whitespace or past 512 characters is refused, not trimmed."""

    @pytest.mark.parametrize(
        "case", MALFORMED_SOURCE_ID_CASES, ids=[case["id"] for case in MALFORMED_SOURCE_ID_CASES]
    )
    def test_a_malformed_one_is_refused_on_create(
        self,
        admin_headers: dict[str, str],
        app: FastAPI,
        client: TestClient,
        case: MalformedSourceIdCase,
    ):
        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "linked",
                "roles": [],
                "source_id": case["source_id"],
                "source_provider": "oidc",
            },
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_source_id"
        assert app.state.group_store.get("linked") is None

    @pytest.mark.parametrize(
        "case", MALFORMED_SOURCE_ID_CASES, ids=[case["id"] for case in MALFORMED_SOURCE_ID_CASES]
    )
    def test_a_malformed_one_is_refused_on_update(
        self,
        admin_headers: dict[str, str],
        app: FastAPI,
        client: TestClient,
        case: MalformedSourceIdCase,
    ):
        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"source_id": case["source_id"], "source_provider": "oidc"},
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_source_id"
        assert app.state.group_store.get("dfe-viewers").source_id == ""

    def test_the_longest_one_is_accepted(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "linked",
                "roles": [],
                "source_id": "g" * 512,
                "source_provider": "oidc",
            },
        )

        assert resp.status_code == 201, resp.text
        assert app.state.group_store.get("linked").source_id == "g" * 512

    def test_a_malformed_one_left_unchanged_is_accepted(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        app.state.group_store.update(name="dfe-viewers", source_id=" SOC ")

        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"description": "Viewers", "source_id": " SOC "},
        )

        assert resp.status_code == 200, resp.text
        assert app.state.group_store.get("dfe-viewers").description == "Viewers"


class TestALinkNamesItsProvider:
    """A new or moved link names the provider whose logins it answers; one an older release left without a provider still answers any."""

    def test_a_create_without_a_provider_is_refused(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={"name": "linked", "roles": [], "source_id": "SOC Analysts"},
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "missing_source_provider"
        assert app.state.group_store.get("linked") is None

    @pytest.mark.parametrize(
        "case", MISSING_PROVIDER_CASES, ids=[case["id"] for case in MISSING_PROVIDER_CASES]
    )
    def test_an_update_leaving_a_link_without_a_provider_is_refused(
        self,
        admin_headers: dict[str, str],
        app: FastAPI,
        client: TestClient,
        case: MissingProviderCase,
    ):
        app.state.group_store.update(name="dfe-viewers", **case["current"])

        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers", headers=admin_headers, json=case["change"]
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "missing_source_provider"
        stored = app.state.group_store.get("dfe-viewers")
        link = {"source_id": stored.source_id, "source_provider": stored.source_provider}
        assert link == case["current"]

    def test_clearing_the_source_id_needs_no_provider(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        app.state.group_store.update(
            name="dfe-viewers", source_id="dfe-viewers", source_provider="oidc"
        )

        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers", headers=admin_headers, json={"source_id": ""}
        )

        assert resp.status_code == 200, resp.text
        stored = app.state.group_store.get("dfe-viewers")
        assert (stored.source_id, stored.source_provider) == ("", "oidc")

    def test_a_legacy_link_without_a_provider_takes_an_edit(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        app.state.group_store.update(name="dfe-viewers", source_id="dfe-viewers")

        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"description": "Viewers"},
        )

        assert resp.status_code == 200, resp.text
        stored = app.state.group_store.get("dfe-viewers")
        assert (stored.description, stored.source_id, stored.source_provider) == (
            "Viewers",
            "dfe-viewers",
            "",
        )


class TestDeletingALinkedGroupNeedsItsRoles:
    """Deleting a linked group takes its roles from every login its source ID answers, member or not.

    operator holds group:* through infra_admin but neither admin nor role:write.
    """

    IDP = {"X-Oidc-Subject": "jane@corp", "X-Oidc-Groups": "sec-admins-idp"}

    @pytest.fixture
    def admin_groups(self, app: FastAPI, client: TestClient) -> None:
        """A memberless admin group linked to sec-admins-idp and an unlinked memberless one."""
        app.state.group_store.create(members=[], name="sec-admins", roles=["admin"])
        app.state.group_store.update(
            name="sec-admins", source_id="sec-admins-idp", source_provider="oidc"
        )
        app.state.group_store.create(members=[], name="sec-retired", roles=["admin"])

    @pytest.mark.parametrize(
        "case", GROUP_DELETE_ROUTE_CASES, ids=[case["id"] for case in GROUP_DELETE_ROUTE_CASES]
    )
    def test_a_memberless_linked_admin_group_is_refused(
        self,
        admin_groups: None,
        app: FastAPI,
        client: TestClient,
        operator_headers: dict[str, str],
        case: GroupDeleteRouteCase,
    ):
        resp = client.delete(f"{case['route']}/sec-admins", headers=operator_headers)

        assert resp.status_code == 403, resp.text
        assert app.state.group_store.get("sec-admins") is not None
        assert "admin" in _roles(client=client, headers=self.IDP)

    @pytest.mark.parametrize(
        "case", GROUP_DELETE_ROUTE_CASES, ids=[case["id"] for case in GROUP_DELETE_ROUTE_CASES]
    )
    def test_a_memberless_unlinked_group_is_deleted(
        self,
        admin_groups: None,
        app: FastAPI,
        client: TestClient,
        operator_headers: dict[str, str],
        case: GroupDeleteRouteCase,
    ):
        resp = client.delete(f"{case['route']}/sec-retired", headers=operator_headers)

        assert resp.status_code == 204, resp.text
        assert app.state.group_store.get("sec-retired") is None


class TestAGroupChangeIsAudited:
    """Each group the API creates, changes or deletes is recorded with the fields it set or changed."""

    def test_a_create_is_recorded(
        self, admin_headers: dict[str, str], audit_events: list[dict], client: TestClient
    ):
        resp = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={"name": "audited", "roles": ["data_viewer"]},
        )

        assert resp.status_code == 201, resp.text
        created = [event for event in audit_events if event["event"] == "resource.group.created"]
        assert [(event["admin_id"], event["resource_name"]) for event in created] == [
            ("admin", "audited")
        ]

    def test_an_update_records_the_link_change(
        self, admin_headers: dict[str, str], audit_events: list[dict], client: TestClient
    ):
        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"source_id": "dfe-viewers", "source_provider": "oidc"},
        )

        assert resp.status_code == 200, resp.text
        updated = [event for event in audit_events if event["event"] == "resource.group.updated"]
        assert [event["details"] for event in updated] == [
            {
                "changed": {
                    "source_id": {"after": "dfe-viewers", "before": ""},
                    "source_provider": {"after": "oidc", "before": ""},
                }
            }
        ]

    def test_a_delete_is_recorded(
        self,
        admin_headers: dict[str, str],
        app: FastAPI,
        audit_events: list[dict],
        client: TestClient,
    ):
        app.state.group_store.create(members=[], name="audited", roles=["data_viewer"])

        resp = client.delete("/api/v1/auth/groups/audited", headers=admin_headers)

        assert resp.status_code == 204, resp.text
        deleted = [event for event in audit_events if event["event"] == "resource.group.deleted"]
        assert [(event["admin_id"], event["resource_name"]) for event in deleted] == [
            ("admin", "audited")
        ]


class TestListGroups:
    """GET /api/v1/auth/groups"""

    def test_list_groups(self, client, admin_headers):
        resp = client.get("/api/v1/auth/groups", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()["items"]
        assert isinstance(data, list)
        # Bootstrap creates dfe-admins at minimum
        names = [g["name"] for g in data]
        assert "dfe-admins" in names

    def test_list_filters_to_own_memberships_without_grant(self, client, viewer_headers):
        # Visibility model: no group:read grant -> only groups the caller
        # is a member of (viewer belongs to dfe-viewers only).
        resp = client.get("/api/v1/auth/groups", headers=viewer_headers)
        assert resp.status_code == 200
        assert [g["name"] for g in resp.json()["items"]] == ["dfe-viewers"]


class TestGetGroup:
    """GET /api/v1/auth/groups/{name}"""

    def test_get_group(self, client, admin_headers):
        resp = client.get("/api/v1/auth/groups/dfe-admins", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "dfe-admins"
        assert "roles" in data
        assert "members" in data

    def test_get_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/auth/groups/nonexistent", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_hidden_without_grant_or_membership(self, client, viewer_headers):
        # Non-visible groups 404 (existence is hidden, not just forbidden).
        resp = client.get("/api/v1/auth/groups/dfe-admins", headers=viewer_headers)
        assert resp.status_code == 404


class TestUpdateGroup:
    """PUT /api/v1/auth/groups/{name}"""

    def test_update_roles(self, client, admin_headers):
        client.post(
            "/api/v1/auth/groups",
            json={"name": "upd-group", "roles": ["data_viewer"]},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/groups/upd-group",
            json={"roles": ["admin", "data_viewer"]},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert set(resp.json()["roles"]) == {"admin", "data_viewer"}

    def test_update_description(self, client, admin_headers):
        client.post(
            "/api/v1/auth/groups",
            json={"name": "desc-group", "roles": ["admin"]},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/groups/desc-group",
            json={"description": "Updated description"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated description"

    def test_update_members(self, client, admin_headers):
        client.post(
            "/api/v1/auth/groups",
            json={"name": "mem-upd-group", "roles": ["data_viewer"]},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/groups/mem-upd-group",
            json={"members": ["admin", "viewer"]},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["members"] == ["admin", "viewer"]

    def test_update_members_syncs_account_groups(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "memsync-user",
                "password": secrets.token_urlsafe(16),
                "email": "memsync-user@example.com",
            },
            headers=admin_headers,
        )
        client.post(
            "/api/v1/auth/groups",
            json={
                "name": "mem-sync-group",
                "roles": ["data_viewer"],
                "members": ["memsync-user"],
            },
            headers=admin_headers,
        )
        account = client.get("/api/v1/auth/accounts/memsync-user", headers=admin_headers)
        assert "mem-sync-group" in account.json()["groups"]

        resp = client.put(
            "/api/v1/auth/groups/mem-sync-group",
            json={"members": []},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        account = client.get("/api/v1/auth/accounts/memsync-user", headers=admin_headers)
        assert account.json()["groups"] == []

    def test_remove_member_syncs_account_groups(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "rm-sync-user",
                "password": secrets.token_urlsafe(16),
                "email": "rm-sync-user@example.com",
                "groups": ["rm-sync-group"],
            },
            headers=admin_headers,
        )
        client.post(
            "/api/v1/auth/groups",
            json={"name": "rm-sync-group", "roles": ["data_viewer"], "members": ["rm-sync-user"]},
            headers=admin_headers,
        )
        resp = client.delete(
            "/api/v1/auth/groups/rm-sync-group/members/rm-sync-user",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        account = client.get("/api/v1/auth/accounts/rm-sync-user", headers=admin_headers)
        assert account.json()["groups"] == []

    def test_a_link_is_stored_and_returned(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"source_id": "dfe-viewers", "source_provider": "oidc"},
        )
        fetched = client.get("/api/v1/auth/groups/dfe-viewers", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        stored = app.state.group_store.get("dfe-viewers")
        assert (stored.source_id, stored.source_provider) == ("dfe-viewers", "oidc")
        link = {"source_id": "dfe-viewers", "source_provider": "oidc"}
        assert {key: resp.json()[key] for key in link} == link
        assert {key: fetched.json()[key] for key in link} == link

    def test_an_omitted_link_is_left_as_it_was(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        app.state.group_store.update(
            name="dfe-viewers", source_id="00g-viewers", source_provider="okta"
        )

        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"description": "Viewers"},
        )

        assert resp.status_code == 200, resp.text
        stored = app.state.group_store.get("dfe-viewers")
        assert (stored.source_id, stored.source_provider) == ("00g-viewers", "okta")

    def test_a_link_is_cleared_with_empty_strings(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient
    ):
        app.state.group_store.update(
            name="dfe-viewers", source_id="00g-viewers", source_provider="okta"
        )

        resp = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"source_id": "", "source_provider": ""},
        )

        assert resp.status_code == 200, resp.text
        stored = app.state.group_store.get("dfe-viewers")
        assert (stored.source_id, stored.source_provider) == ("", "")

    def test_update_nonexistent_returns_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/auth/groups/ghost",
            json={"roles": ["admin"]},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_update_requires_admin(self, client, viewer_headers):
        resp = client.put(
            "/api/v1/auth/groups/dfe-admins",
            json={"roles": ["admin"]},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestAddMember:
    """POST /api/v1/auth/groups/{name}/members"""

    def test_add_member(self, client, admin_headers):
        client.post(
            "/api/v1/auth/groups",
            json={"name": "member-group", "roles": ["data_viewer"]},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/auth/groups/member-group/members",
            json={"username": "admin"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert "admin" in resp.json()["members"]

    def test_add_member_idempotent(self, client, admin_headers):
        client.post(
            "/api/v1/auth/groups",
            json={"name": "idem-group", "roles": ["data_viewer"]},
            headers=admin_headers,
        )
        client.post(
            "/api/v1/auth/groups/idem-group/members",
            json={"username": "admin"},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/auth/groups/idem-group/members",
            json={"username": "admin"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        # Should appear only once
        assert resp.json()["members"].count("admin") == 1

    def test_add_member_nonexistent_group_returns_404(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/groups/ghost/members",
            json={"username": "admin"},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_add_member_requires_admin(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/auth/groups/dfe-admins/members",
            json={"username": "viewer"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestRemoveMember:
    """DELETE /api/v1/auth/groups/{name}/members/{username}"""

    def test_remove_member(self, client, admin_headers):
        client.post(
            "/api/v1/auth/groups",
            json={"name": "rm-group", "roles": ["data_viewer"]},
            headers=admin_headers,
        )
        client.post(
            "/api/v1/auth/groups/rm-group/members",
            json={"username": "admin"},
            headers=admin_headers,
        )
        resp = client.delete(
            "/api/v1/auth/groups/rm-group/members/admin",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert "admin" not in resp.json()["members"]

    def test_remove_member_nonexistent_group_returns_404(self, client, admin_headers):
        resp = client.delete(
            "/api/v1/auth/groups/ghost/members/admin",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_remove_member_requires_admin(self, client, viewer_headers):
        resp = client.delete(
            "/api/v1/auth/groups/dfe-admins/members/admin",
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestDeleteGroup:
    """DELETE /api/v1/auth/groups/{name}"""

    def test_delete_group(self, client, admin_headers):
        client.post(
            "/api/v1/auth/groups",
            json={"name": "del-group", "roles": ["data_viewer"]},
            headers=admin_headers,
        )
        resp = client.delete("/api/v1/auth/groups/del-group", headers=admin_headers)
        assert resp.status_code == 204

        # Confirm it's gone
        resp = client.get("/api/v1/auth/groups/del-group", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_group_with_members_returns_409(self, client, admin_headers):
        client.post(
            "/api/v1/auth/groups",
            json={"name": "busy-group", "roles": ["data_viewer"], "members": ["admin"]},
            headers=admin_headers,
        )
        resp = client.delete("/api/v1/auth/groups/busy-group", headers=admin_headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_delete_nonexistent_returns_404(self, client, admin_headers):
        resp = client.delete("/api/v1/auth/groups/ghost", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_requires_admin(self, client, viewer_headers):
        resp = client.delete("/api/v1/auth/groups/dfe-admins", headers=viewer_headers)
        assert resp.status_code == 403


# ── The protected-name floor (issue #505) ────────────────────


@pytest.mark.parametrize("username", ["admin", "breakglass"])
class TestProtectedGroupMembership:
    """The native group router refuses de-roling a recovery credential."""

    def test_remove_member_is_refused(self, recovery_accounts, client, admin_headers, username):
        resp = client.delete(
            f"/api/v1/auth/groups/dfe-admins/members/{username}",
            headers=admin_headers,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "protected_account"
        assert username in recovery_accounts.state.group_store.get("dfe-admins").members
        assert "dfe-admins" in recovery_accounts.state.account_store.get(username).groups

    def test_member_replacement_that_drops_it_is_refused(
        self, recovery_accounts, client, admin_headers, username
    ):
        resp = client.put(
            "/api/v1/auth/groups/dfe-admins",
            json={"members": ["operator"]},
            headers=admin_headers,
        )
        assert resp.status_code == 403, resp.text
        assert username in recovery_accounts.state.group_store.get("dfe-admins").members

    def test_delete_of_the_admin_group_is_refused(
        self, recovery_accounts, client, admin_headers, username
    ):
        """GroupStore.delete refuses a non-empty group, and the members cannot come out."""
        resp = client.delete("/api/v1/auth/groups/dfe-admins", headers=admin_headers)
        assert resp.status_code == 409, resp.text
        assert recovery_accounts.state.group_store.get("dfe-admins") is not None
        assert username in recovery_accounts.state.group_store.get("dfe-admins").members

    def test_adding_another_member_still_works(
        self, recovery_accounts, client, admin_headers, username
    ):
        resp = client.post(
            "/api/v1/auth/groups/dfe-admins/members",
            json={"username": "operator"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        members = recovery_accounts.state.group_store.get("dfe-admins").members
        assert "operator" in members
        assert username in members
