#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_protected_accounts.py
#  Purpose:      Tests for the protected-name floor in the account and group stores
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The floor under the local admin and break-glass, enforced at the stores.

Covers the five writes that would remove an operator's way back in -- disable,
delete, rename, drop from the admin group, take the admin role off that group --
plus the reconcile paths that must
keep working: password reset, contact edits, adding a group, and the
``allow_protected`` writes admin retirement and the deploy-repo hydration make.
"""

import pytest

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.auth.breakglass import GROUP as RECOVERY_GROUP
from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.protected_accounts import ProtectedAccountError, resolve_floor

PROTECTED = ("admin", BREAKGLASS)


@pytest.fixture
def store(tmp_path):
    return AccountStore(tmp_path / "accounts")


@pytest.fixture
def groups(tmp_path):
    store = GroupStore(tmp_path / "groups")
    store.create(RECOVERY_GROUP, roles=["admin"])
    store.create("dfe-viewers", roles=["data_viewer"])
    return store


def _seed(store: AccountStore, username: str) -> Account:
    return store.create(username, "s3cret-Pw", groups=[RECOVERY_GROUP])


class TestFloorResolution:
    def test_default_names_are_admin_and_breakglass(self):
        assert resolve_floor().usernames == {"admin", BREAKGLASS}

    def test_admin_override_moves_the_floor(self):
        floor = resolve_floor("root")
        assert floor.usernames == {"root", BREAKGLASS}
        assert not floor.is_protected("admin")

    def test_group_is_the_admin_role_group(self):
        assert resolve_floor().group == RECOVERY_GROUP

    def test_role_is_the_admin_role(self):
        assert resolve_floor().role == "admin"


class TestAccountStoreRefuses:
    @pytest.mark.parametrize("username", PROTECTED)
    def test_disable_is_refused(self, store, username):
        _seed(store, username)
        with pytest.raises(ProtectedAccountError):
            store.update(username, enabled=False)
        assert store.get(username).enabled is True

    @pytest.mark.parametrize("username", PROTECTED)
    def test_block_is_refused(self, store, username):
        _seed(store, username)
        with pytest.raises(ProtectedAccountError):
            store.update(username, blocked=True)
        assert store.get(username).blocked is False

    @pytest.mark.parametrize("username", PROTECTED)
    def test_delete_is_refused(self, store, username):
        _seed(store, username)
        with pytest.raises(ProtectedAccountError):
            store.delete(username)
        assert store.get(username) is not None

    @pytest.mark.parametrize("username", PROTECTED)
    def test_dropping_the_admin_group_is_refused(self, store, username):
        _seed(store, username)
        with pytest.raises(ProtectedAccountError):
            store.update(username, groups=["dfe-viewers"])
        assert store.get(username).groups == [RECOVERY_GROUP]

    @pytest.mark.parametrize("username", PROTECTED)
    def test_empty_group_list_is_refused(self, store, username):
        _seed(store, username)
        with pytest.raises(ProtectedAccountError):
            store.update(username, groups=[])

    def test_a_string_group_payload_is_refused(self, store):
        """A str is iterable, so it must not satisfy the floor by substring."""
        _seed(store, "admin")
        with pytest.raises(ProtectedAccountError):
            store.update("admin", groups=RECOVERY_GROUP)

    @pytest.mark.parametrize("username", PROTECTED)
    def test_put_of_a_disabled_record_is_refused(self, store, username):
        _seed(store, username)
        with pytest.raises(ProtectedAccountError):
            store.put(
                Account(
                    username=username,
                    password_hash="$2b$12$x",
                    enabled=False,
                    groups=[RECOVERY_GROUP],
                )
            )
        assert store.get(username).enabled is True

    @pytest.mark.parametrize("username", PROTECTED)
    def test_put_outside_the_admin_group_is_refused(self, store, username):
        with pytest.raises(ProtectedAccountError):
            store.put(Account(username=username, password_hash="$2b$12$x", enabled=True, groups=[]))

    def test_the_refusal_names_the_account_and_a_way_through(self, store):
        _seed(store, BREAKGLASS)
        with pytest.raises(ProtectedAccountError) as exc:
            store.update(BREAKGLASS, enabled=False)
        assert BREAKGLASS in str(exc.value)
        assert "breakglass.enabled" in str(exc.value)


class TestAccountStoreStillAllows:
    @pytest.mark.parametrize("username", PROTECTED)
    def test_password_reset(self, store, username):
        _seed(store, username)
        store.reset_password(username, "another-Pw-1")
        assert store.verify_password(username, "another-Pw-1")

    @pytest.mark.parametrize("username", PROTECTED)
    def test_contact_fields(self, store, username):
        _seed(store, username)
        account = store.update(username, email="ops@example.com", phone="+61000")
        assert account.email == "ops@example.com"

    @pytest.mark.parametrize("username", PROTECTED)
    def test_enabling_and_adding_a_group(self, store, username):
        _seed(store, username)
        account = store.update(username, enabled=True, groups=[RECOVERY_GROUP, "dfe-viewers"])
        assert account.groups == [RECOVERY_GROUP, "dfe-viewers"]

    def test_an_unprotected_account_is_untouched(self, store):
        store.create("alice", "s3cret-Pw", groups=[RECOVERY_GROUP])
        store.update("alice", enabled=False, groups=[])
        store.delete("alice")
        assert store.get("alice") is None

    @pytest.mark.parametrize("username", PROTECTED)
    def test_allow_protected_disables_for_admin_retirement(self, store, username):
        _seed(store, username)
        assert store.update(username, enabled=False, allow_protected=True).enabled is False

    @pytest.mark.parametrize("username", PROTECTED)
    def test_allow_protected_puts_a_disabled_record_for_hydration(self, store, username):
        _seed(store, username)
        store.put(
            Account(username=username, password_hash="$2b$12$x", enabled=False, groups=[]),
            allow_protected=True,
        )
        assert store.get(username).enabled is False

    @pytest.mark.parametrize("username", PROTECTED)
    def test_allow_protected_deletes_for_the_e2e_wipe(self, store, username):
        _seed(store, username)
        store.delete(username, allow_protected=True)
        assert store.get(username) is None

    def test_a_missing_protected_name_still_answers_keyerror(self, store):
        with pytest.raises(KeyError):
            store.delete(BREAKGLASS)


class TestGroupStoreRefuses:
    @pytest.mark.parametrize("username", PROTECTED)
    def test_remove_member_from_the_admin_group(self, groups, username):
        groups.add_member(RECOVERY_GROUP, username)
        with pytest.raises(ProtectedAccountError):
            groups.remove_member(RECOVERY_GROUP, username)
        assert username in groups.get(RECOVERY_GROUP).members

    @pytest.mark.parametrize("username", PROTECTED)
    def test_member_replacement_that_drops_it(self, groups, username):
        groups.add_member(RECOVERY_GROUP, username)
        groups.add_member(RECOVERY_GROUP, "alice")
        with pytest.raises(ProtectedAccountError):
            groups.update(RECOVERY_GROUP, members=["alice"])
        assert username in groups.get(RECOVERY_GROUP).members

    def test_a_batch_removal_refuses_before_applying_any_of_it(self, groups):
        groups.add_member(RECOVERY_GROUP, "alice")
        groups.add_member(RECOVERY_GROUP, BREAKGLASS)
        with pytest.raises(ProtectedAccountError):
            groups.protected.check_member_removal(RECOVERY_GROUP, ["alice", BREAKGLASS])
        assert groups.get(RECOVERY_GROUP).members == ["alice", BREAKGLASS]

    def test_the_admin_group_cannot_be_emptied_so_cannot_be_deleted(self, groups):
        groups.add_member(RECOVERY_GROUP, BREAKGLASS)
        with pytest.raises(ProtectedAccountError):
            groups.remove_member(RECOVERY_GROUP, BREAKGLASS)
        with pytest.raises(ValueError):
            groups.delete(RECOVERY_GROUP)

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"roles": ["infra_admin"]}, id="admin-role-dropped"),
            pytest.param({"roles": []}, id="roles-emptied"),
            pytest.param({"roles": "admin"}, id="roles-as-a-string"),
            pytest.param({"scope": "org:acme"}, id="moved-to-an-org"),
        ],
    )
    def test_taking_the_admin_role_off_the_admin_group(self, groups, change):
        """Every recovery credential in it would authenticate with no role."""
        with pytest.raises(ProtectedAccountError):
            groups.update(RECOVERY_GROUP, **change)
        stored = groups.get(RECOVERY_GROUP)
        assert (stored.roles, stored.scope) == (["admin"], "system")


class TestGroupStoreStillAllows:
    def test_removing_an_unprotected_member(self, groups):
        groups.add_member(RECOVERY_GROUP, "alice")
        groups.remove_member(RECOVERY_GROUP, "alice")
        assert groups.get(RECOVERY_GROUP).members == []

    def test_removing_a_protected_member_from_another_group(self, groups):
        groups.add_member("dfe-viewers", BREAKGLASS)
        groups.remove_member("dfe-viewers", BREAKGLASS)
        assert groups.get("dfe-viewers").members == []

    def test_replacing_members_while_keeping_the_protected_one(self, groups):
        groups.add_member(RECOVERY_GROUP, BREAKGLASS)
        groups.add_member(RECOVERY_GROUP, "alice")
        group = groups.update(RECOVERY_GROUP, members=[BREAKGLASS, "bob"])
        assert group.members == [BREAKGLASS, "bob"]

    def test_role_and_description_edits(self, groups):
        groups.add_member(RECOVERY_GROUP, BREAKGLASS)
        group = groups.update(RECOVERY_GROUP, description="Full admin", roles=["admin"])
        assert group.description == "Full admin"

    def test_a_role_added_beside_admin(self, groups):
        group = groups.update(RECOVERY_GROUP, roles=["admin", "data_viewer"], scope="system")
        assert group.roles == ["admin", "data_viewer"]

    def test_another_groups_roles_and_scope(self, groups):
        group = groups.update("dfe-viewers", roles=[], scope="org:acme")
        assert (group.roles, group.scope) == ([], "org:acme")

    def test_allow_protected_changes_the_admin_groups_roles(self, groups):
        group = groups.update(RECOVERY_GROUP, roles=["infra_admin"], allow_protected=True)
        assert group.roles == ["infra_admin"]

    def test_allow_protected_removes_for_the_reconcile(self, groups):
        groups.add_member(RECOVERY_GROUP, BREAKGLASS)
        groups.remove_member(RECOVERY_GROUP, BREAKGLASS, allow_protected=True)
        assert groups.get(RECOVERY_GROUP).members == []


class TestRenamedAdmin:
    def test_the_configured_name_is_protected_and_the_default_is_not(self, tmp_path):
        store = AccountStore(tmp_path / "accounts", admin_name="root")
        store.create("root", "s3cret-Pw", groups=[RECOVERY_GROUP])
        store.create("admin", "s3cret-Pw", groups=[RECOVERY_GROUP])
        with pytest.raises(ProtectedAccountError):
            store.update("root", enabled=False)
        assert store.update("admin", enabled=False).enabled is False
