"""Tests for field map resolver — two-tier merge logic."""

import pytest

from dfe_engine.fieldmap.models import FieldMap
from dfe_engine.fieldmap.registry import FieldMapError, FieldMapRegistry
from dfe_engine.fieldmap.resolver import (
    resolve_field,
    resolve_field_map,
    resolve_registry_mappings,
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
            source="windows_audit",
            mappings={"User": "account_name"},
        )
        result = resolve_field_map(source_map=source)
        assert result == {"User": "account_name"}

    def test_source_overrides_default(self):
        default = _make_map(mappings={"User": "user_name", "Image": "process_name"})
        source = _make_map(
            source="windows_audit",
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
            FieldMap(standard="sigma", source="windows_audit", mappings={"A": "convention_a"})
        )
        fm_registry.save_map(
            FieldMap(standard="sigma", source="corp_pin", mappings={"A": "pinned_a"})
        )

    def test_source_name_convention(self, fm_registry):
        self._seed(fm_registry)
        result = resolve_registry_mappings(fm_registry, "sigma", "windows_audit")
        assert result == {"A": "convention_a", "B": "b"}

    def test_field_map_pin_bare_name(self, fm_registry):
        """A field_map pin REPLACES the source-name convention layer."""
        self._seed(fm_registry)
        result = resolve_registry_mappings(
            fm_registry, "sigma", "windows_audit", field_map="corp_pin"
        )
        assert result == {"A": "pinned_a", "B": "b"}

    def test_field_map_pin_standard_slash_name(self, fm_registry):
        self._seed(fm_registry)
        result = resolve_registry_mappings(
            fm_registry, "sigma", "windows_audit", field_map="sigma/corp_pin"
        )
        assert result == {"A": "pinned_a", "B": "b"}

    def test_field_map_standard_mismatch_raises(self, fm_registry):
        self._seed(fm_registry)
        with pytest.raises(FieldMapError, match="ecs"):
            resolve_registry_mappings(
                fm_registry, "sigma", "windows_audit", field_map="ecs/corp_pin"
            )

    def test_missing_pinned_map_leaves_default_only(self, fm_registry):
        self._seed(fm_registry)
        result = resolve_registry_mappings(
            fm_registry, "sigma", "windows_audit", field_map="no_such_map"
        )
        assert result == {"A": "default_a", "B": "b"}

    def test_no_maps_returns_empty(self, fm_registry):
        assert resolve_registry_mappings(fm_registry, "sigma", "windows_audit") == {}


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
