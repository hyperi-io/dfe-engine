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

import pytest

from dfe_engine.api.deps import create_access_token


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

    def test_removing_a_member_needs_no_role(self, client, app, operator_headers):
        app.state.group_store.add_member("dfe-admins", "viewer")

        resp = client.delete(
            "/api/v1/auth/groups/dfe-admins/members/viewer", headers=operator_headers
        )

        assert resp.status_code == 200, resp.text
        assert "viewer" not in app.state.group_store.get("dfe-admins").members


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
