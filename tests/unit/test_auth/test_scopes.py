#  Project:      dfe-engine
#  File:         test_scopes.py
#  Purpose:      Scope coverage matrix + group scope validation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Scope model tests: coverage semantics and group scope handling."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dfe_engine.auth import Scope
from dfe_engine.auth.groups import Group, GroupStore, validate_group_scope

SYSTEM = Scope()
ORG_ACME = Scope(type="org", id="acme")
ORG_GLOBEX = Scope(type="org", id="globex")
GROUP_ACME = Scope(type="group", id="analysts", org="acme")
GROUP_GLOBEX = Scope(type="group", id="analysts", org="globex")
GROUP_SYSTEM = Scope(type="group", id="analysts")
USER_ALICE = Scope(type="user", id="alice")
USER_BOB = Scope(type="user", id="bob")


class TestScopeCovers:
    def test_system_covers_everything(self):
        for requested in (SYSTEM, ORG_ACME, GROUP_ACME, GROUP_SYSTEM, USER_ALICE):
            assert SYSTEM.covers(requested)

    def test_org_covers_itself_only(self):
        assert ORG_ACME.covers(ORG_ACME)
        assert not ORG_ACME.covers(ORG_GLOBEX)
        assert not ORG_ACME.covers(SYSTEM)

    def test_org_covers_its_own_groups(self):
        assert ORG_ACME.covers(GROUP_ACME)
        assert not ORG_ACME.covers(GROUP_GLOBEX)

    def test_org_never_covers_system_groups(self):
        # Org admins must not manage system-wide groups.
        assert not ORG_ACME.covers(GROUP_SYSTEM)

    def test_org_does_not_cover_user_scope(self):
        # User-scope data (e.g. personal prefs) is the owner's; only
        # system scope reaches it.
        assert not ORG_ACME.covers(USER_ALICE)

    def test_group_covers_identical_group_only(self):
        assert GROUP_ACME.covers(GROUP_ACME)
        assert not GROUP_ACME.covers(GROUP_GLOBEX)
        assert not GROUP_ACME.covers(GROUP_SYSTEM)
        assert not GROUP_SYSTEM.covers(GROUP_ACME)
        assert not GROUP_ACME.covers(ORG_ACME)

    def test_user_covers_self_only(self):
        assert USER_ALICE.covers(USER_ALICE)
        assert not USER_ALICE.covers(USER_BOB)
        assert not USER_ALICE.covers(SYSTEM)

    def test_str_forms(self):
        assert str(SYSTEM) == "system"
        assert str(ORG_ACME) == "org:acme"
        assert str(GROUP_ACME) == "group:acme/analysts"
        assert str(GROUP_SYSTEM) == "group:analysts"
        assert str(USER_ALICE) == "user:alice"


class TestGroupScopeValidation:
    def test_valid_scopes(self):
        assert validate_group_scope("system") == "system"
        assert validate_group_scope("org:acme") == "org:acme"

    @pytest.mark.parametrize("bad", ["", "org:", "org: ", "acme", "user:bob", "System"])
    def test_invalid_scopes(self, bad):
        with pytest.raises(ValueError):
            validate_group_scope(bad)

    def test_group_default_scope_is_system(self):
        group = Group(name="g")
        assert group.scope == "system"
        assert group.scope_org == ""

    def test_group_org_scope(self):
        group = Group(name="g", scope="org:acme")
        assert group.scope_org == "acme"

    def test_group_rejects_invalid_scope(self):
        with pytest.raises(ValidationError):
            Group(name="g", scope="everywhere")


class TestGroupStoreScope:
    def test_create_persists_scope(self, tmp_path):
        store = GroupStore(tmp_path)
        store.create("acme-analysts", roles=["data_analyst"], scope="org:acme")
        reloaded = GroupStore(tmp_path).get("acme-analysts")
        assert reloaded is not None
        assert reloaded.scope == "org:acme"
        assert reloaded.scope_org == "acme"

    def test_create_defaults_to_system(self, tmp_path):
        store = GroupStore(tmp_path)
        store.create("ops", roles=[])
        group = store.get("ops")
        assert group is not None
        assert group.scope == "system"

    def test_create_rejects_invalid_scope(self, tmp_path):
        store = GroupStore(tmp_path)
        with pytest.raises(ValueError):
            store.create("bad", roles=[], scope="org:")

    def test_update_validates_scope(self, tmp_path):
        store = GroupStore(tmp_path)
        store.create("ops", roles=[])
        with pytest.raises(ValueError):
            store.update("ops", scope="nonsense")
        updated = store.update("ops", scope="org:acme")
        assert updated.scope == "org:acme"

    def test_legacy_group_file_without_scope_loads_as_system(self, tmp_path):
        (tmp_path / "legacy.yaml").write_text("description: old\nroles: [admin]\nmembers: []\n")
        group = GroupStore(tmp_path).get("legacy")
        assert group is not None
        assert group.scope == "system"
