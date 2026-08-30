"""Tests for FieldMap Pydantic model."""

import pytest

from dfe_engine.fieldmap.models import DEFAULT_MAP_NAME, FieldMap

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_field_map(
    standard: str = "sigma",
    source: str | None = None,
    mappings: dict[str, str] | None = None,
    **kwargs,
) -> FieldMap:
    """Create a minimal FieldMap for testing."""
    return FieldMap(
        standard=standard,
        source=source,
        mappings=mappings or {},
        **kwargs,
    )


# ---------------------------------------------------------------
# Basic Creation
# ---------------------------------------------------------------


class TestFieldMapBasic:
    def test_minimal_creation(self):
        fm = _make_field_map()
        assert fm.standard == "sigma"
        assert fm.source is None
        assert fm.mappings == {}
        assert fm.version is None
        assert fm.description is None
        assert fm.inherits is None

    def test_full_creation(self):
        fm = _make_field_map(
            standard="ecs",
            source="windows-audit",
            mappings={"source.ip": "source_ip"},
            version="8.11",
            description="ECS for windows",
            inherits="_default",
        )
        assert fm.standard == "ecs"
        assert fm.source == "windows-audit"
        assert fm.mappings == {"source.ip": "source_ip"}
        assert fm.version == "8.11"
        assert fm.description == "ECS for windows"
        assert fm.inherits == "_default"

    def test_to_yaml_dict_excludes_none(self):
        fm = _make_field_map(mappings={"User": "user_name"})
        data = fm.to_yaml_dict()
        assert "version" not in data
        assert "description" not in data
        assert "inherits" not in data
        assert "source" not in data
        assert data["standard"] == "sigma"
        assert data["mappings"] == {"User": "user_name"}

    def test_to_yaml_dict_excludes_empty_mappings(self):
        fm = _make_field_map()
        data = fm.to_yaml_dict()
        assert "mappings" not in data

    def test_to_yaml_dict_round_trip(self):
        fm = _make_field_map(
            standard="ecs",
            source="windows-audit",
            mappings={"source.ip": "source_ip", "user.name": "user_name"},
            version="8.11",
        )
        data = fm.to_yaml_dict()
        restored = FieldMap.model_validate(data)
        assert restored.standard == fm.standard
        assert restored.source == fm.source
        assert restored.mappings == fm.mappings
        assert restored.version == fm.version

    def test_from_dict(self):
        data = {
            "standard": "cim",
            "source": "linux-syslog",
            "mappings": {"src_ip": "source_ip"},
        }
        fm = FieldMap.model_validate(data)
        assert fm.standard == "cim"
        assert fm.source == "linux-syslog"


# ---------------------------------------------------------------
# Validation
# ---------------------------------------------------------------


class TestFieldMapValidation:
    def test_standard_forced_lowercase(self):
        fm = _make_field_map(standard="SIGMA")
        assert fm.standard == "sigma"

    def test_source_forced_lowercase(self):
        fm = _make_field_map(source="Windows-Audit")
        assert fm.source == "windows-audit"

    def test_invalid_standard_raises(self):
        with pytest.raises(ValueError, match="must match"):
            _make_field_map(standard="123bad")

    def test_invalid_standard_special_chars(self):
        with pytest.raises(ValueError, match="must match"):
            _make_field_map(standard="sigma-ecs")

    def test_invalid_source_raises(self):
        """``source`` holds a _source label, so it answers to the source-name rule."""
        with pytest.raises(ValueError, match="DNS-1123 label"):
            _make_field_map(source="123bad")

    def test_invalid_source_special_chars(self):
        with pytest.raises(ValueError, match="DNS-1123 label"):
            _make_field_map(source="windows.audit")

    def test_source_underscore_refused_like_a_source_name(self):
        """The two ends cannot drift: an underscore is refused in both places."""
        with pytest.raises(ValueError, match="use '-' instead of '_'"):
            _make_field_map(source="windows_audit")

    def test_standard_required(self):
        with pytest.raises(Exception):
            FieldMap(mappings={"a": "b"})


# ---------------------------------------------------------------
# Properties
# ---------------------------------------------------------------


class TestFieldMapProperties:
    def test_registry_key_default(self):
        fm = _make_field_map(standard="sigma")
        assert fm.registry_key == f"sigma/{DEFAULT_MAP_NAME}"

    def test_registry_key_source(self):
        fm = _make_field_map(standard="ecs", source="windows-audit")
        assert fm.registry_key == "ecs/windows-audit"

    def test_is_default_true(self):
        fm = _make_field_map()
        assert fm.is_default is True

    def test_is_default_false(self):
        fm = _make_field_map(source="windows-audit")
        assert fm.is_default is False
