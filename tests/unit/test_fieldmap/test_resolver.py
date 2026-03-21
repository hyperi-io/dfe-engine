"""Tests for field map resolver — two-tier merge logic."""

from dfe_engine.fieldmap.models import FieldMap
from dfe_engine.fieldmap.resolver import resolve_field, resolve_field_map

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
