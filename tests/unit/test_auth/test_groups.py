#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_groups.py
#  Purpose:      Tests for GroupStore YAML-backed group CRUD and role resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families
from scalo.logger import logger
from scalo.metrics import create_metrics

from dfe_engine.auth.groups import (
    GROUPS_SKIPPED,
    DocuStoreGroupStore,
    Group,
    GroupExistsError,
    GroupMetrics,
    GroupStore,
)


def test_the_stores_annotations_can_be_read():
    """Both stores define list(), which a bare list[...] in their signatures would name."""
    assert GroupStore.list.__annotations__["return"] == list[Group]
    assert GroupStore.create.__annotations__["members"] == list[str] | None
    assert DocuStoreGroupStore.resolve_roles_for_member.__annotations__["return"] == list[str]


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

    @pytest.mark.parametrize("scope", ["org:../elsewhere/outside", "org:a/b", "org:acme\n"])
    def test_an_org_scope_names_an_org_the_registry_could_hold(self, scope):
        """The org part is looked up in the org registry, so it follows the org name rule."""
        with pytest.raises(ValueError, match="Group scope"):
            Group(name="climber", scope=scope)


_SKIPPED = "stored group skipped: not a valid group"
_BAD_SCOPE = "roles: [admin]\nmembers: [bob]\nscope: org:../elsewhere/outside\n"
_VALID = "roles: [admin]\nmembers: [bob]\n"

_UNLOADABLE = [
    pytest.param(_BAD_SCOPE, "invalid", id="bad-scope"),
    pytest.param("roles: [admin\nmembers: [bob]\n", "unreadable", id="malformed-yaml"),
    pytest.param("- admin\n- bob\n", "not_a_mapping", id="not-a-mapping"),
    pytest.param("just a string\n", "not_a_mapping", id="a-scalar"),
]


@contextmanager
def _warnings() -> Iterator[list[dict]]:
    """The skip warnings logged inside the block, with their fields."""
    seen: list[dict] = []
    handler = logger.add(
        lambda m: seen.append({"event": m.record["message"], **m.record["extra"]}),
        level="WARNING",
        format="{message}",
    )
    try:
        yield seen
    finally:
        logger.remove(handler)


def _skip_counts(manager) -> dict[str, float]:
    counts: dict[str, float] = {}
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == GROUPS_SKIPPED:
                counts[sample.labels["reason"]] = sample.value
    return counts


class TestAStoredGroupFileThatDoesNotLoad:
    """One file no Group can be made from must not take every other group down with it."""

    @pytest.fixture
    def groups_dir(self, tmp_path) -> Path:
        return tmp_path / "groups"

    @pytest.fixture
    def manager(self):
        return create_metrics("test", backend="prometheus", enable_auto_update=False)

    @pytest.fixture
    def store(self, groups_dir, manager):
        store = GroupStore(groups_dir, metrics=GroupMetrics(manager))
        store.create("analysts", roles=["data_analyst"], members=["bob"])
        return store

    @pytest.mark.parametrize(("content", "reason"), _UNLOADABLE)
    def test_every_other_group_still_loads_and_the_bad_one_is_absent(
        self, store, groups_dir, content, reason
    ):
        (groups_dir / "climber.yaml").write_text(content, encoding="utf-8")

        assert [g.name for g in store.list()] == ["analysts"]
        assert store.get("climber") is None
        assert store.get("analysts").roles == ["data_analyst"]
        assert store.resolve_roles_for_member("bob") == ["data_analyst"]

    @pytest.mark.parametrize(("content", "reason"), _UNLOADABLE)
    def test_the_skip_is_logged_and_counted_once_naming_the_file(
        self, store, groups_dir, manager, content, reason
    ):
        (groups_dir / "climber.yaml").write_text(content, encoding="utf-8")

        with _warnings() as seen:
            store.list()
            store.list()
            store.get("climber")

        skipped = [w for w in seen if w["event"] == _SKIPPED]
        assert [(Path(w["path"]).name, w["reason"]) for w in skipped] == [("climber.yaml", reason)]
        assert _skip_counts(manager) == {reason: 1.0}

    def test_a_file_that_loads_again_is_reported_again_when_it_next_breaks(
        self, store, groups_dir, manager
    ):
        stored = groups_dir / "climber.yaml"

        with _warnings() as seen:
            stored.write_text(_BAD_SCOPE, encoding="utf-8")
            store.list()
            stored.write_text(_VALID, encoding="utf-8")
            assert [g.name for g in store.list()] == ["analysts", "climber"]
            stored.write_text(_BAD_SCOPE, encoding="utf-8")
            store.list()
            store.list()

        assert [w["event"] for w in seen].count(_SKIPPED) == 2
        assert _skip_counts(manager) == {"invalid": 2.0}

    def test_creating_one_over_it_is_refused_as_existing(self, store, groups_dir):
        """get() reports it absent, so create() is what keeps the file from being replaced."""
        (groups_dir / "climber.yaml").write_text(_BAD_SCOPE, encoding="utf-8")

        with pytest.raises(GroupExistsError):
            store.create("climber", roles=[])

        assert (groups_dir / "climber.yaml").read_text(encoding="utf-8") == _BAD_SCOPE


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

    def test_create_duplicate_raises_value_error(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("admins", roles=["admin"])
        with pytest.raises(ValueError, match="admins"):
            store.create("admins", roles=["viewer"])

    def test_create_rejects_unsafe_name(self, tmp_path):
        # Group name becomes the {name}.yaml filename stem - reject traversal /
        # separators / a trailing newline.
        store = GroupStore(tmp_path / "groups")
        for bad in ("../evil", "a/b", "..", ".hidden", "with space", "bad\n", ""):
            with pytest.raises(ValueError, match="Invalid group name"):
                store.create(bad, roles=["admin"])

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


class TestAGroupNameThatIsAPath:
    """X-Oidc-Groups values reach these lookups straight from the proxy."""

    OUTSIDE = "../elsewhere/outside"

    @pytest.fixture
    def outside(self, tmp_path):
        """A group file one directory over, where :attr:`OUTSIDE` resolves from the store."""
        GroupStore(tmp_path / "elsewhere").create("outside", roles=["admin"])
        return tmp_path / "elsewhere" / "outside.yaml"

    def test_get_reads_no_file(self, tmp_path, outside):
        assert GroupStore(tmp_path / "groups").get(self.OUTSIDE) is None

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(lambda s, n: s.update(n, roles=["viewer"]), id="update"),
            pytest.param(lambda s, n: s.set_attributes(n, {"k": "v"}), id="set_attributes"),
            pytest.param(lambda s, n: s.add_member(n, "mallory"), id="add_member"),
            pytest.param(lambda s, n: s.remove_member(n, "mallory"), id="remove_member"),
            pytest.param(lambda s, n: s.delete(n), id="delete"),
        ],
    )
    def test_a_change_raises_and_leaves_the_file(self, tmp_path, outside, change):
        before = outside.read_bytes()

        with pytest.raises(KeyError):
            change(GroupStore(tmp_path / "groups"), self.OUTSIDE)

        assert outside.read_bytes() == before


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


class TestGroupStoreBySourceId:
    def test_only_groups_with_source_id_appear(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        store.create("named-only", roles=["admin"])  # no source_id
        store.create("synced", roles=["data_viewer"])
        store.update("synced", source_id="guid-123", source_provider="entra")
        index = store.by_source_id()
        assert set(index) == {"guid-123"}
        assert index["guid-123"].name == "synced"
        assert index["guid-123"].roles == ["data_viewer"]

    def test_empty_store(self, tmp_path):
        store = GroupStore(tmp_path / "groups")
        assert store.by_source_id() == {}


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
        store.create("acme-viewers", roles=["org_viewer"])
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
