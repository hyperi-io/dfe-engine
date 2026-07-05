#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_groups.py
#  Purpose:      Tests for GroupStore YAML-backed group CRUD and role resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from dfe_engine.auth.groups import Group, GroupStore


class TestGroupModel:
    def test_defaults(self):
        g = Group(name="admins")
        assert g.name == "admins"
        assert g.description == ""
        assert g.roles == []
        assert g.members == []

    def test_with_all_fields(self):
        g = Group(name="ops", description="Ops team", roles=["operator"], members=["alice"])
        assert g.roles == ["operator"]
        assert g.members == ["alice"]


class TestGroupStoreCreate:
    def test_create_success(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        group = store.create("admins", roles=["admin"], description="Admin group")
        assert group.name == "admins"
        assert group.roles == ["admin"]
        assert group.description == "Admin group"

    def test_create_writes_yaml_file(self, tmp_path):
        groups_dir = tmp_path / "groups"
        store = GroupStore(groups_dir)
        store.create("ops", roles=["operator"])
        assert (groups_dir / "ops.yaml").exists()

    def test_create_with_org_ids(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        group = store.create("acme-viewers", roles=["org_analyst"], org_ids=["acme-tenant"])
        assert group.org_ids == ["acme-tenant"]

    def test_create_with_org_ids_persists_in_one_write(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create(
            "acme-viewers",
            roles=["org_analyst"],
            members=["alice"],
            org_ids=["acme-tenant"],
        )
        reloaded = GroupStore(tmp_path / "groups").get("acme-viewers")
        assert reloaded.org_ids == ["acme-tenant"]
        assert reloaded.members == ["alice"]

    def test_create_without_org_ids_defaults_empty(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        group = store.create("ops", roles=["operator"])
        assert group.org_ids == []

    def test_create_duplicate_raises_value_error(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("admins", roles=["admin"])
        with pytest.raises(ValueError, match="admins"):
            store.create("admins", roles=["viewer"])

    def test_create_creates_dir_if_not_exists(self, tmp_path):
        groups_dir = tmp_path / "new" / "groups"
        assert not groups_dir.exists()
        store = GroupStore(groups_dir)
        store.create("test", roles=[])
        assert groups_dir.exists()

    def test_create_name_not_stored_in_yaml(self, tmp_path):
        """Group name is the filename stem, NOT stored as a field in the YAML."""
        from dfe_engine.yaml_utils import yaml_load

        store = GroupStore(tmp_path / "groups")
        store.create("mygroup", roles=["viewer"])
        data = yaml_load(tmp_path / "groups" / "mygroup.yaml")
        assert "name" not in data


class TestGroupStoreGet:
    def test_get_existing(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=["operator"], description="Ops team")
        group = store.get("ops")
        assert group is not None
        assert group.name == "ops"
        assert group.roles == ["operator"]
        assert group.description == "Ops team"

    def test_get_nonexistent_returns_none(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        assert store.get("nonexistent") is None

    def test_get_sets_name_from_filename(self, tmp_path):
        """Name is derived from filename, not stored in the file."""
        store = GroupStore(tmp_path / "groups")
        store.create("mygroup", roles=[])
        group = store.get("mygroup")
        assert group is not None
        assert group.name == "mygroup"


class TestGroupStoreList:
    def test_list_empty(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        assert store.list() == []

    def test_list_multiple_sorted(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("zebra", roles=[])
        store.create("alpha", roles=[])
        store.create("middle", roles=[])
        groups = store.list()
        assert [g.name for g in groups] == ["alpha", "middle", "zebra"]

    def test_list_returns_group_objects(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=["operator"])
        groups = store.list()
        assert len(groups) == 1
        assert isinstance(groups[0], Group)
        assert groups[0].roles == ["operator"]


class TestGroupStoreUpdate:
    def test_update_roles(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=["viewer"])
        updated = store.update("ops", roles=["operator", "viewer"])
        assert updated.roles == ["operator", "viewer"]

    def test_update_description(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=[], description="old")
        updated = store.update("ops", description="new description")
        assert updated.description == "new description"

    def test_update_persists_to_file(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=["viewer"])
        store.update("ops", roles=["operator"])
        # Re-read via fresh get
        group = store.get("ops")
        assert group is not None
        assert group.roles == ["operator"]

    def test_update_nonexistent_raises_key_error(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        with pytest.raises(KeyError, match="nonexistent"):
            store.update("nonexistent", roles=["admin"])

    def test_update_preserves_unchanged_fields(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=["viewer"], description="Ops team")
        store.add_member("ops", "alice")
        updated = store.update("ops", roles=["operator"])
        assert updated.description == "Ops team"
        assert "alice" in updated.members


class TestGroupStoreDelete:
    def test_delete_existing(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=[])
        store.delete("ops")
        assert store.get("ops") is None

    def test_delete_removes_file(self, tmp_path):
        groups_dir = tmp_path / "groups"
        store = GroupStore(groups_dir)
        store.create("ops", roles=[])
        store.delete("ops")
        assert not (groups_dir / "ops.yaml").exists()

    def test_delete_nonexistent_raises_key_error(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        with pytest.raises(KeyError, match="nonexistent"):
            store.delete("nonexistent")

    def test_delete_with_members_raises_value_error(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=[])
        store.add_member("ops", "alice")
        with pytest.raises(ValueError, match="member"):
            store.delete("ops")


class TestGroupStoreAddMember:
    def test_add_member(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=[])
        store.add_member("ops", "alice")
        group = store.get("ops")
        assert group is not None
        assert "alice" in group.members

    def test_add_member_idempotent(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=[])
        store.add_member("ops", "alice")
        store.add_member("ops", "alice")
        group = store.get("ops")
        assert group is not None
        assert group.members.count("alice") == 1

    def test_add_member_group_not_found_raises_key_error(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        with pytest.raises(KeyError, match="nonexistent"):
            store.add_member("nonexistent", "alice")

    def test_add_multiple_members(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=[])
        store.add_member("ops", "alice")
        store.add_member("ops", "bob")
        group = store.get("ops")
        assert group is not None
        assert set(group.members) == {"alice", "bob"}


class TestGroupStoreRemoveMember:
    def test_remove_member(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=[])
        store.add_member("ops", "alice")
        store.remove_member("ops", "alice")
        group = store.get("ops")
        assert group is not None
        assert "alice" not in group.members

    def test_remove_member_not_in_group_is_noop(self, tmp_path):
        """Removing a user not in the group does not raise an error."""
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=[])
        store.remove_member("ops", "nobody")
        group = store.get("ops")
        assert group is not None
        assert group.members == []

    def test_remove_member_group_not_found_raises_key_error(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        with pytest.raises(KeyError, match="nonexistent"):
            store.remove_member("nonexistent", "alice")

    def test_remove_member_persists(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=[])
        store.add_member("ops", "alice")
        store.add_member("ops", "bob")
        store.remove_member("ops", "alice")
        group = store.get("ops")
        assert group is not None
        assert group.members == ["bob"]


class TestGroupStoreResolveRoles:
    def test_user_in_no_groups(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=["operator"])
        roles = store.resolve_roles_for_member("alice")
        assert roles == []

    def test_user_in_one_group(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=["operator"])
        store.add_member("ops", "alice")
        roles = store.resolve_roles_for_member("alice")
        assert roles == ["operator"]

    def test_user_in_multiple_groups_union_of_roles(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("ops", roles=["operator"])
        store.create("admins", roles=["admin", "operator"])
        store.add_member("ops", "alice")
        store.add_member("admins", "alice")
        roles = store.resolve_roles_for_member("alice")
        # Sorted, unique
        assert roles == sorted({"operator", "admin"})

    def test_resolve_roles_returns_sorted_unique(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("g1", roles=["viewer", "operator"])
        store.create("g2", roles=["operator", "admin"])
        store.add_member("g1", "bob")
        store.add_member("g2", "bob")
        roles = store.resolve_roles_for_member("bob")
        assert roles == sorted({"viewer", "operator", "admin"})
        assert len(roles) == len(set(roles))  # no duplicates

    def test_empty_store_returns_empty_list(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        assert store.resolve_roles_for_member("alice") == []


class TestGroupSourceProvider:
    def test_group_defaults_source_provider_empty(self):
        g = Group(name="test")
        assert g.source_provider == ""
        assert g.source_id == ""

    def test_group_with_source_provider(self):
        g = Group(
            name="engineering", source_provider="google-workspace", source_id="eng@example.com"
        )
        assert g.source_provider == "google-workspace"
        assert g.source_id == "eng@example.com"

    def test_source_provider_persists_through_create_get_cycle(self, tmp_path):
        """source_provider and source_id survive a create/get round-trip."""
        store = GroupStore(tmp_path / "groups")
        store.create("engineering", roles=["operator"])
        store.update("engineering", source_provider="google-workspace", source_id="grp-abc123")
        group = store.get("engineering")
        assert group is not None
        assert group.source_provider == "google-workspace"
        assert group.source_id == "grp-abc123"

    def test_source_provider_persists_through_fresh_store_instance(self, tmp_path):
        """source_provider survives across separate GroupStore instances (written to YAML)."""
        groups_dir = tmp_path / "groups"
        store1 = GroupStore(groups_dir)
        store1.create("ops", roles=[])
        store1.update("ops", source_provider="entra-id", source_id="obj-xyz")

        store2 = GroupStore(groups_dir)
        group = store2.get("ops")
        assert group is not None
        assert group.source_provider == "entra-id"
        assert group.source_id == "obj-xyz"

    def test_group_org_ids(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("acme-viewers", roles=["org_analyst"])
        store.update("acme-viewers", org_ids=["acme"])
        group = store.get("acme-viewers")
        assert group.org_ids == ["acme"]

    def test_existing_groups_without_source_provider_still_load(self, tmp_path):
        """Old YAML files without source_provider/source_id load with defaults."""
        from dfe_engine.yaml_utils import yaml_dump

        groups_dir = tmp_path / "groups"
        groups_dir.mkdir()
        # Write a YAML file that doesn't have source_provider (old format)
        yaml_dump(
            {"description": "legacy group", "roles": ["viewer"], "members": []},
            groups_dir / "legacy.yaml",
        )

        store = GroupStore(groups_dir)
        group = store.get("legacy")
        assert group is not None
        assert group.source_provider == ""
        assert group.source_id == ""
        assert group.roles == ["viewer"]
