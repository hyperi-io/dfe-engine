"""Tests for field map resolver — two-tier merge logic."""

import pytest

from dfe_engine.fieldmap.models import FieldMap
from dfe_engine.fieldmap.registry import FieldMapError, FieldMapRegistry
from dfe_engine.fieldmap.resolver import (
    resolve_field,
    resolve_field_map,
    resolve_registry_mappings,
    split_by_columns,
)

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_map(
    standard: str = "sigma",
    source: str | None = None,
    mappings: dict[str, str] | None = None,
) -> FieldMap:
    return FieldMap(standard=standard, source=source, mappings=mappings or {})


# ---------------------------------------------------------------
# resolve_field_map
# ---------------------------------------------------------------


class TestResolveFieldMap:
    def test_both_none_returns_empty(self):
        assert resolve_field_map(None, None) == {}

    def test_default_only(self):
        default = _make_map(mappings={"User": "user_name", "Image": "process_name"})
        result = resolve_field_map(default_map=default)
        assert result == {"User": "user_name", "Image": "process_name"}

    def test_source_only(self):
        source = _make_map(
            source="windows-audit",
            mappings={"User": "account_name"},
        )
        result = resolve_field_map(source_map=source)
        assert result == {"User": "account_name"}

    def test_source_overrides_default(self):
        default = _make_map(mappings={"User": "user_name", "Image": "process_name"})
        source = _make_map(
            source="windows-audit",
            mappings={"User": "account_name"},
        )
        result = resolve_field_map(default, source)
        assert result["User"] == "account_name"
        assert result["Image"] == "process_name"

    def test_default_entries_preserved(self):
        default = _make_map(mappings={"A": "a", "B": "b", "C": "c"})
        source = _make_map(source="src", mappings={"B": "b_override"})
        result = resolve_field_map(default, source)
        assert result == {"A": "a", "B": "b_override", "C": "c"}

    def test_disjoint_maps_merge(self):
        default = _make_map(mappings={"A": "a", "B": "b"})
        source = _make_map(source="src", mappings={"C": "c", "D": "d"})
        result = resolve_field_map(default, source)
        assert result == {"A": "a", "B": "b", "C": "c", "D": "d"}

    def test_empty_source_returns_default(self):
        default = _make_map(mappings={"User": "user_name"})
        source = _make_map(source="src", mappings={})
        result = resolve_field_map(default, source)
        assert result == {"User": "user_name"}

    def test_empty_default_returns_source(self):
        default = _make_map(mappings={})
        source = _make_map(source="src", mappings={"User": "account_name"})
        result = resolve_field_map(default, source)
        assert result == {"User": "account_name"}

    def test_both_empty_returns_empty(self):
        default = _make_map(mappings={})
        source = _make_map(source="src", mappings={})
        result = resolve_field_map(default, source)
        assert result == {}


# ---------------------------------------------------------------
# resolve_field
# ---------------------------------------------------------------


# ---------------------------------------------------------------
# resolve_registry_mappings (incl. the SourceView field_map pin)
# ---------------------------------------------------------------


@pytest.fixture
def fm_registry(tmp_path):
    FieldMapRegistry.reset_instance()
    reg = FieldMapRegistry(
        field_maps_directory=tmp_path / "field-maps",
        writable=True,
        refresh_interval=0,
    )
    yield reg
    reg.close()
    FieldMapRegistry.reset_instance()


class TestResolveRegistryMappings:
    def _seed(self, fm_registry):
        fm_registry.save_map(FieldMap(standard="sigma", mappings={"A": "default_a", "B": "b"}))
        fm_registry.save_map(
            FieldMap(standard="sigma", source="windows-audit", mappings={"A": "convention_a"})
        )
        fm_registry.save_map(
            FieldMap(standard="sigma", source="corp-pin", mappings={"A": "pinned_a"})
        )

    def test_source_name_convention(self, fm_registry):
        self._seed(fm_registry)
        result = resolve_registry_mappings(fm_registry, "sigma", "windows-audit")
        assert result == {"A": "convention_a", "B": "b"}

    def test_field_map_pin_bare_name(self, fm_registry):
        """A field_map pin REPLACES the source-name convention layer."""
        self._seed(fm_registry)
        result = resolve_registry_mappings(
            fm_registry, "sigma", "windows-audit", field_map="corp-pin"
        )
        assert result == {"A": "pinned_a", "B": "b"}

    def test_field_map_pin_standard_slash_name(self, fm_registry):
        self._seed(fm_registry)
        result = resolve_registry_mappings(
            fm_registry, "sigma", "windows-audit", field_map="sigma/corp-pin"
        )
        assert result == {"A": "pinned_a", "B": "b"}

    def test_field_map_standard_mismatch_raises(self, fm_registry):
        self._seed(fm_registry)
        with pytest.raises(FieldMapError, match="ecs"):
            resolve_registry_mappings(
                fm_registry, "sigma", "windows-audit", field_map="ecs/corp-pin"
            )

    def test_missing_pinned_map_leaves_default_only(self, fm_registry):
        self._seed(fm_registry)
        result = resolve_registry_mappings(
            fm_registry, "sigma", "windows-audit", field_map="no_such_map"
        )
        assert result == {"A": "default_a", "B": "b"}

    def test_no_maps_returns_empty(self, fm_registry):
        assert resolve_registry_mappings(fm_registry, "sigma", "windows-audit") == {}


class TestResolveField:
    def test_mapped_field(self):
        resolved = {"User": "user_name", "Image": "process_name"}
        assert resolve_field("User", resolved) == "user_name"

    def test_unmapped_field_passthrough(self):
        resolved = {"User": "user_name"}
        assert resolve_field("CommandLine", resolved) == "CommandLine"

    def test_empty_map_passthrough(self):
        assert resolve_field("anything", {}) == "anything"

    def test_case_sensitive(self):
        resolved = {"User": "user_name"}
        assert resolve_field("user", resolved) == "user"
        assert resolve_field("User", resolved) == "user_name"


class TestSplitByColumns:
    def test_splits_on_target_column(self):
        mappings = {
            "EventID": "event_code",
            "Image": "process_executable",
            "@timestamp": "_timestamp",
        }
        usable, dropped = split_by_columns(mappings, ["event_code", "_timestamp", "_raw"])
        assert usable == {"EventID": "event_code", "@timestamp": "_timestamp"}
        assert dropped == {"Image": "process_executable"}

    def test_matches_the_column_not_the_field(self):
        """`network_protocol` is a field name here, not the column it reads."""
        usable, dropped = split_by_columns(
            {"network_protocol": "network_transport"}, ["network_protocol"]
        )
        assert usable == {}
        assert dropped == {"network_protocol": "network_transport"}

    def test_column_names_are_case_sensitive(self):
        usable, dropped = split_by_columns({"User": "user_name"}, ["User_Name"])
        assert usable == {}
        assert dropped == {"User": "user_name"}

    def test_empty_inputs(self):
        assert split_by_columns({}, ["a"]) == ({}, {})
        assert split_by_columns({"A": "a"}, []) == ({}, {"A": "a"})
