#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_group_crud.py
#  Purpose:      Tests for group CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for POST/GET/PUT/DELETE /api/v1/auth/groups endpoints."""

from __future__ import annotations


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
                "password": "pw",
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
                "password": "pw",
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
