#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_scim.py
#  Purpose:      Tests for the SCIM 2.0 provisioning face over the account/group stores
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for /api/v1/scim/v2 (SCIM 2.0 Users/Groups + discovery).

Covers:
  - pure mapper round-trips (Account<->User, Group<->Group)
  - User CRUD via TestClient, verifying it lands in the AccountStore
  - Group CRUD via TestClient, verifying it lands in the GroupStore
  - PATCH deprovision (active=false) + group membership PATCH
  - the protected-name floor: every write route refuses a recovery credential
  - discovery endpoints (ServiceProviderConfig / ResourceTypes / Schemas)
"""

from __future__ import annotations

import secrets

import pytest

from dfe_engine.auth.accounts import Account
from dfe_engine.auth.bootstrap import MIN_ADMIN_PASSWORD_LENGTH
from dfe_engine.auth.breakglass import GROUP as RECOVERY_GROUP
from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS
from dfe_engine.auth.groups import Group
from dfe_engine.auth.scim_mapping import (
    account_to_scim_user,
    group_to_scim_group,
    scim_group_to_group_fields,
    scim_user_to_account_fields,
)

BASE = "/api/v1/scim/v2"

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"


# ── Pure mapper round-trips ──────────────────────────────────


class TestMapperRoundTrips:
    def test_account_to_user_to_fields(self):
        account = Account(
            username="alice",
            password_hash="x",
            enabled=False,
            groups=["admins"],
            external_id="ext-42",
            created_at="2026-07-01T00:00:00+00:00",
            updated_at="2026-07-02T00:00:00+00:00",
        )
        user = account_to_scim_user(account, groups=["admins"])
        assert user.user_name == "alice"
        assert user.id == "alice"
        assert user.active is False
        assert user.external_id == "ext-42"
        assert [m.value for m in user.groups] == ["admins"]

        fields = scim_user_to_account_fields(user)
        assert fields["username"] == "alice"
        assert fields["enabled"] is False
        assert fields["external_id"] == "ext-42"
        assert fields["source_provider"] == "scim"

    def test_user_active_defaults_true_when_absent(self):
        from scim2_models import User as ScimUser

        fields = scim_user_to_account_fields(ScimUser(user_name="bob"))
        assert fields["enabled"] is True

    def test_group_to_scim_to_fields(self):
        group = Group(name="engineers", members=["alice", "bob"], source_id="g-7")
        scim_group = group_to_scim_group(group)
        assert scim_group.display_name == "engineers"
        assert scim_group.id == "engineers"
        assert scim_group.external_id == "g-7"
        assert [m.value for m in scim_group.members] == ["alice", "bob"]

        fields = scim_group_to_group_fields(scim_group)
        assert fields["name"] == "engineers"
        assert fields["members"] == ["alice", "bob"]
        assert fields["source_id"] == "g-7"
        assert fields["source_provider"] == "scim"


# ── User CRUD via the API ────────────────────────────────────


class TestScimUsers:
    def test_create_user_lands_in_store(self, app, client, admin_headers):
        resp = client.post(
            f"{BASE}/Users",
            json={
                "schemas": [USER_SCHEMA],
                "userName": "scim-alice",
                "externalId": "okta-1",
                "active": True,
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["userName"] == "scim-alice"
        assert body["id"] == "scim-alice"
        assert body["externalId"] == "okta-1"
        assert body["active"] is True
        assert USER_SCHEMA in body["schemas"]
        assert resp.headers["content-type"].startswith("application/scim+json")

        # Verify it actually persisted to the AccountStore (gitops YAML).
        account = app.state.account_store.get("scim-alice")
        assert account is not None
        assert account.enabled is True
        assert account.external_id == "okta-1"
        assert account.source_provider == "scim"

    def test_a_password_under_the_floor_is_refused(self, app, client, admin_headers):
        short = secrets.token_urlsafe(16)[: MIN_ADMIN_PASSWORD_LENGTH - 1]
        resp = client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "scim-short", "password": short},
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["scimType"] == "invalidValue"
        assert str(MIN_ADMIN_PASSWORD_LENGTH) in resp.json()["detail"]
        assert short not in resp.text
        assert app.state.account_store.get("scim-short") is None

    def test_a_password_at_the_floor_is_stored(self, app, client, admin_headers):
        at_floor = secrets.token_urlsafe(16)[:MIN_ADMIN_PASSWORD_LENGTH]
        resp = client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "scim-floor", "password": at_floor},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        assert app.state.account_store.verify_password("scim-floor", at_floor)

    def test_create_duplicate_returns_409(self, client, admin_headers):
        payload = {"schemas": [USER_SCHEMA], "userName": "dup-user"}
        client.post(f"{BASE}/Users", json=payload, headers=admin_headers)
        resp = client.post(f"{BASE}/Users", json=payload, headers=admin_headers)
        assert resp.status_code == 409
        assert resp.json()["scimType"] == "uniqueness"

    def test_get_user(self, client, admin_headers):
        client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "scim-get"},
            headers=admin_headers,
        )
        resp = client.get(f"{BASE}/Users/scim-get", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["userName"] == "scim-get"

    def test_get_missing_user_404(self, client, admin_headers):
        resp = client.get(f"{BASE}/Users/nope", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]

    def test_list_users(self, client, admin_headers):
        client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "scim-list-1"},
            headers=admin_headers,
        )
        resp = client.get(f"{BASE}/Users", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert LIST_SCHEMA in body["schemas"]
        names = {u["userName"] for u in body["Resources"]}
        assert "scim-list-1" in names
        assert body["totalResults"] >= 1

    def test_list_users_filter(self, client, admin_headers):
        client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "filter-me"},
            headers=admin_headers,
        )
        resp = client.get(
            f"{BASE}/Users", params={"filter": 'userName eq "filter-me"'}, headers=admin_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["totalResults"] == 1
        assert body["Resources"][0]["userName"] == "filter-me"

    def test_patch_deactivate(self, app, client, admin_headers):
        client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "deprov", "active": True},
            headers=admin_headers,
        )
        resp = client.patch(
            f"{BASE}/Users/deprov",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["active"] is False
        assert app.state.account_store.get("deprov").enabled is False

    def test_put_replaces_attributes(self, app, client, admin_headers):
        client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "put-user", "active": True},
            headers=admin_headers,
        )
        resp = client.put(
            f"{BASE}/Users/put-user",
            json={
                "schemas": [USER_SCHEMA],
                "userName": "put-user",
                "active": False,
                "externalId": "new-ext",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200
        account = app.state.account_store.get("put-user")
        assert account.enabled is False
        assert account.external_id == "new-ext"

    def test_delete_user(self, app, client, admin_headers):
        client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "gone"},
            headers=admin_headers,
        )
        resp = client.delete(f"{BASE}/Users/gone", headers=admin_headers)
        assert resp.status_code == 204
        assert app.state.account_store.get("gone") is None

    def test_create_requires_write_scope(self, client, viewer_headers):
        resp = client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "blocked"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


# ── A refused body never echoes what it carried ──────────────


def _echoes(secret: str, text: str, window: int = 8) -> bool:
    """Whether any run of *window* characters of *secret* appears in *text*.

    pydantic truncates a long input in its error string, so a partial echo counts.
    """
    return any(secret[i : i + window] in text for i in range(len(secret) - window + 1))


# Bodies scim2_models refuses, each carrying a password.
_REFUSED_USER_BODIES = {
    "no_username": lambda pw: {"schemas": [USER_SCHEMA], "password": pw},
    "password_list": lambda pw: {
        "schemas": [USER_SCHEMA],
        "userName": "leak-list",
        "password": [pw],
    },
    "body_list": lambda pw: [{"schemas": [USER_SCHEMA], "userName": "leak-body", "password": pw}],
    "wrong_urn": lambda pw: {
        "schemas": ["urn:example:wrong"],
        "userName": "leak-urn",
        "password": pw,
    },
    "password_object": lambda pw: {
        "schemas": [USER_SCHEMA],
        "userName": "leak-object",
        "password": {"value": pw},
    },
}


class TestScimRefusalsCarryNoPassword:
    @pytest.mark.parametrize("shape", sorted(_REFUSED_USER_BODIES))
    def test_create(self, client, admin_headers, shape):
        password = secrets.token_urlsafe(24)
        resp = client.post(
            f"{BASE}/Users", json=_REFUSED_USER_BODIES[shape](password), headers=admin_headers
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["scimType"] == "invalidValue"
        assert resp.json()["detail"].startswith("Invalid User")
        assert not _echoes(password, resp.text)
        assert "model_fields" not in resp.text

    def test_put(self, client, admin_headers):
        client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "leak-put"},
            headers=admin_headers,
        )
        password = secrets.token_urlsafe(24)
        resp = client.put(
            f"{BASE}/Users/leak-put",
            json=_REFUSED_USER_BODIES["no_username"](password),
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert not _echoes(password, resp.text)

    def test_patch(self, client, admin_headers):
        client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": "leak-patch"},
            headers=admin_headers,
        )
        password = secrets.token_urlsafe(24)
        resp = client.patch(
            f"{BASE}/Users/leak-patch",
            json={
                "schemas": ["urn:example:wrong"],
                "Operations": [{"op": "replace", "path": "password", "value": password}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert not _echoes(password, resp.text)

    def test_group_create(self, client, admin_headers):
        secret = secrets.token_urlsafe(24)
        resp = client.post(
            f"{BASE}/Groups",
            json={"schemas": ["urn:example:wrong"], "displayName": "leak-group", "x": secret},
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert not _echoes(secret, resp.text)


# ── Group CRUD via the API ───────────────────────────────────


class TestScimGroups:
    def _make_user(self, client, admin_headers, name):
        client.post(
            f"{BASE}/Users",
            json={"schemas": [USER_SCHEMA], "userName": name},
            headers=admin_headers,
        )

    def test_create_group_lands_in_store(self, app, client, admin_headers):
        self._make_user(client, admin_headers, "gm-1")
        resp = client.post(
            f"{BASE}/Groups",
            json={
                "schemas": [GROUP_SCHEMA],
                "displayName": "scim-team",
                "externalId": "grp-1",
                "members": [{"value": "gm-1"}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["displayName"] == "scim-team"
        assert body["externalId"] == "grp-1"
        assert [m["value"] for m in body["members"]] == ["gm-1"]

        group = app.state.group_store.get("scim-team")
        assert group is not None
        assert group.members == ["gm-1"]
        assert group.source_id == "grp-1"
        assert group.source_provider == "scim"
        # membership mirrored onto the account
        assert "scim-team" in app.state.account_store.get("gm-1").groups

    def test_create_duplicate_group_returns_409(self, client, admin_headers):
        payload = {"schemas": [GROUP_SCHEMA], "displayName": "dup-group"}
        client.post(f"{BASE}/Groups", json=payload, headers=admin_headers)

        resp = client.post(f"{BASE}/Groups", json=payload, headers=admin_headers)

        assert resp.status_code == 409, resp.text
        assert resp.json()["scimType"] == "uniqueness"

    def test_create_over_a_group_file_that_does_not_load_returns_409(
        self, app, client, admin_headers
    ):
        """get() reports the file absent, so create() is what keeps it from being replaced."""
        stored = app.state.group_store._dir / "climber.yaml"
        stored.write_text("roles: [admin]\nscope: org:../elsewhere/outside\n", encoding="utf-8")

        resp = client.post(
            f"{BASE}/Groups",
            json={"schemas": [GROUP_SCHEMA], "displayName": "climber"},
            headers=admin_headers,
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["scimType"] == "uniqueness"
        assert "org:../elsewhere/outside" in stored.read_text(encoding="utf-8")

    def test_get_and_list_groups(self, client, admin_headers):
        client.post(
            f"{BASE}/Groups",
            json={"schemas": [GROUP_SCHEMA], "displayName": "listable"},
            headers=admin_headers,
        )
        got = client.get(f"{BASE}/Groups/listable", headers=admin_headers)
        assert got.status_code == 200
        assert got.json()["displayName"] == "listable"

        listing = client.get(f"{BASE}/Groups", headers=admin_headers)
        assert listing.status_code == 200
        names = {g["displayName"] for g in listing.json()["Resources"]}
        assert "listable" in names

    def test_patch_add_and_remove_member(self, app, client, admin_headers):
        self._make_user(client, admin_headers, "pm-1")
        self._make_user(client, admin_headers, "pm-2")
        client.post(
            f"{BASE}/Groups",
            json={"schemas": [GROUP_SCHEMA], "displayName": "patch-grp"},
            headers=admin_headers,
        )
        add = client.patch(
            f"{BASE}/Groups/patch-grp",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "add",
                        "path": "members",
                        "value": [{"value": "pm-1"}, {"value": "pm-2"}],
                    }
                ],
            },
            headers=admin_headers,
        )
        assert add.status_code == 200, add.text
        assert set(app.state.group_store.get("patch-grp").members) == {"pm-1", "pm-2"}

        remove = client.patch(
            f"{BASE}/Groups/patch-grp",
            json={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [{"op": "remove", "path": 'members[value eq "pm-1"]'}],
            },
            headers=admin_headers,
        )
        assert remove.status_code == 200, remove.text
        assert app.state.group_store.get("patch-grp").members == ["pm-2"]

    def test_put_replaces_members(self, app, client, admin_headers):
        self._make_user(client, admin_headers, "rp-1")
        self._make_user(client, admin_headers, "rp-2")
        client.post(
            f"{BASE}/Groups",
            json={
                "schemas": [GROUP_SCHEMA],
                "displayName": "replace-grp",
                "members": [{"value": "rp-1"}],
            },
            headers=admin_headers,
        )
        resp = client.put(
            f"{BASE}/Groups/replace-grp",
            json={
                "schemas": [GROUP_SCHEMA],
                "displayName": "replace-grp",
                "members": [{"value": "rp-2"}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert app.state.group_store.get("replace-grp").members == ["rp-2"]
        # account membership mirrored both ways
        assert "replace-grp" not in app.state.account_store.get("rp-1").groups
        assert "replace-grp" in app.state.account_store.get("rp-2").groups

    def test_delete_group_with_members(self, app, client, admin_headers):
        self._make_user(client, admin_headers, "dg-1")
        client.post(
            f"{BASE}/Groups",
            json={
                "schemas": [GROUP_SCHEMA],
                "displayName": "del-grp",
                "members": [{"value": "dg-1"}],
            },
            headers=admin_headers,
        )
        resp = client.delete(f"{BASE}/Groups/del-grp", headers=admin_headers)
        assert resp.status_code == 204
        assert app.state.group_store.get("del-grp") is None
        assert "del-grp" not in app.state.account_store.get("dg-1").groups


# ── The protected-name floor (issue #505) ────────────────────


PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


@pytest.mark.parametrize("username", ["admin", BREAKGLASS])
class TestScimProtectedNames:
    """An IdP holding account:write may not remove either account's way back in."""

    def test_patch_active_false_is_refused(
        self, recovery_accounts, client, admin_headers, username
    ):
        resp = client.patch(
            f"{BASE}/Users/{username}",
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 403, resp.text
        assert resp.headers["content-type"].startswith("application/scim+json")
        assert resp.json()["scimType"] == "mutability"
        assert recovery_accounts.state.account_store.get(username).enabled is True

    def test_patch_bare_active_false_is_refused(
        self, recovery_accounts, client, admin_headers, username
    ):
        """The pathless form IdPs also send: {"value": {"active": false}}."""
        resp = client.patch(
            f"{BASE}/Users/{username}",
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "replace", "value": {"active": False}}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 403, resp.text
        assert recovery_accounts.state.account_store.get(username).enabled is True

    def test_put_active_false_is_refused(self, recovery_accounts, client, admin_headers, username):
        resp = client.put(
            f"{BASE}/Users/{username}",
            json={"schemas": [USER_SCHEMA], "userName": username, "active": False},
            headers=admin_headers,
        )
        assert resp.status_code == 403, resp.text
        assert recovery_accounts.state.account_store.get(username).enabled is True

    def test_delete_user_is_refused(self, recovery_accounts, client, admin_headers, username):
        resp = client.delete(f"{BASE}/Users/{username}", headers=admin_headers)
        assert resp.status_code == 403, resp.text
        assert recovery_accounts.state.account_store.get(username) is not None

    def test_group_patch_remove_is_refused(
        self, recovery_accounts, client, admin_headers, username
    ):
        resp = client.patch(
            f"{BASE}/Groups/{RECOVERY_GROUP}",
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "remove", "path": f'members[value eq "{username}"]'}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 403, resp.text
        group = recovery_accounts.state.group_store.get(RECOVERY_GROUP)
        assert username in group.members
        assert RECOVERY_GROUP in recovery_accounts.state.account_store.get(username).groups

    def test_group_put_dropping_it_is_refused(
        self, recovery_accounts, client, admin_headers, username
    ):
        resp = client.put(
            f"{BASE}/Groups/{RECOVERY_GROUP}",
            json={
                "schemas": [GROUP_SCHEMA],
                "displayName": RECOVERY_GROUP,
                "members": [{"value": "nobody"}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 403, resp.text
        assert username in recovery_accounts.state.group_store.get(RECOVERY_GROUP).members

    def test_group_delete_is_refused(self, recovery_accounts, client, admin_headers, username):
        resp = client.delete(f"{BASE}/Groups/{RECOVERY_GROUP}", headers=admin_headers)
        assert resp.status_code == 403, resp.text
        assert recovery_accounts.state.group_store.get(RECOVERY_GROUP) is not None
        assert username in recovery_accounts.state.group_store.get(RECOVERY_GROUP).members


class TestScimProtectedNamesStillWork:
    def test_a_protected_account_keeps_its_contact_fields_editable(
        self, recovery_accounts, client, admin_headers
    ):
        resp = client.put(
            f"{BASE}/Users/{BREAKGLASS}",
            json={"schemas": [USER_SCHEMA], "userName": BREAKGLASS, "active": True},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert recovery_accounts.state.account_store.get(BREAKGLASS).enabled is True

    def test_an_unprotected_member_of_the_admin_group_still_comes_out(
        self, recovery_accounts, client, admin_headers
    ):
        recovery_accounts.state.group_store.add_member(RECOVERY_GROUP, "operator")
        resp = client.patch(
            f"{BASE}/Groups/{RECOVERY_GROUP}",
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "remove", "path": 'members[value eq "operator"]'}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert "operator" not in recovery_accounts.state.group_store.get(RECOVERY_GROUP).members

    def test_a_refused_batch_leaves_the_other_members_in_place(
        self, recovery_accounts, client, admin_headers
    ):
        """The remove-with-no-filter form clears the group; nothing may be detached."""
        recovery_accounts.state.group_store.add_member(RECOVERY_GROUP, "operator")
        resp = client.patch(
            f"{BASE}/Groups/{RECOVERY_GROUP}",
            json={
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "remove", "path": "members"}],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 403, resp.text
        members = recovery_accounts.state.group_store.get(RECOVERY_GROUP).members
        assert {"admin", BREAKGLASS, "operator"} <= set(members)


# ── Discovery ────────────────────────────────────────────────


class TestScimDiscovery:
    def test_service_provider_config(self, client):
        resp = client.get(f"{BASE}/ServiceProviderConfig")
        assert resp.status_code == 200
        body = resp.json()
        assert body["patch"]["supported"] is True
        assert body["bulk"]["supported"] is False
        assert body["authenticationSchemes"][0]["type"] == "oauthbearertoken"

    def test_resource_types(self, client):
        resp = client.get(f"{BASE}/ResourceTypes")
        assert resp.status_code == 200
        ids = {r["id"] for r in resp.json()["Resources"]}
        assert ids == {"User", "Group"}

    def test_schemas(self, client):
        resp = client.get(f"{BASE}/Schemas")
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["Resources"]}
        assert USER_SCHEMA in ids
        assert GROUP_SCHEMA in ids
