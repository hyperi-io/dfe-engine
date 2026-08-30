"""Tests for FieldMapRegistry — CRUD, seed, and listing."""

import pytest

from dfe_engine.fieldmap.models import FieldMap
from dfe_engine.fieldmap.registry import (
    FieldMapError,
    FieldMapNotFoundError,
    FieldMapRegistry,
    FieldMapValidationError,
)

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_field_map(
    standard: str = "sigma",
    source: str | None = None,
    mappings: dict[str, str] | None = None,
    **kwargs,
) -> FieldMap:
    return FieldMap(
        standard=standard,
        source=source,
        mappings=mappings or {},
        **kwargs,
    )


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------


@pytest.fixture
def field_maps_dir(tmp_path):
    d = tmp_path / "field-maps"
    d.mkdir()
    return d


@pytest.fixture
def registry(field_maps_dir):
    FieldMapRegistry.reset_instance()
    reg = FieldMapRegistry(
        field_maps_directory=field_maps_dir,
        writable=True,
        refresh_interval=0,
    )
    yield reg
    reg.close()
    FieldMapRegistry.reset_instance()


# ---------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------


class TestCRUD:
    def test_save_and_get_default(self, registry):
        fm = _make_field_map(mappings={"User": "user_name"})
        registry.save_map(fm)

        loaded = registry.get_map("sigma")
        assert loaded.standard == "sigma"
        assert loaded.source is None
        assert loaded.mappings == {"User": "user_name"}

    def test_save_and_get_source_specific(self, registry):
        fm = _make_field_map(
            source="windows-audit",
            mappings={"User": "account_name"},
        )
        registry.save_map(fm)

        loaded = registry.get_map("sigma", "windows-audit")
        assert loaded.source == "windows-audit"
        assert loaded.mappings == {"User": "account_name"}

    def test_save_from_dict(self, registry):
        data = {
            "standard": "ecs",
            "source": "linux-syslog",
            "mappings": {"source.ip": "source_ip"},
        }
        result = registry.save_map(data)
        assert isinstance(result, FieldMap)
        assert result.standard == "ecs"

        loaded = registry.get_map("ecs", "linux-syslog")
        assert loaded.mappings == {"source.ip": "source_ip"}

    def test_save_invalid_dict_raises(self, registry):
        with pytest.raises(FieldMapValidationError):
            registry.save_map({"standard": "123bad"})

    def test_get_not_found_raises(self, registry):
        with pytest.raises(FieldMapNotFoundError):
            registry.get_map("sigma", "nonexistent")

    def test_get_default_not_found_raises(self, registry):
        with pytest.raises(FieldMapNotFoundError):
            registry.get_map("sigma")

    def test_update_existing(self, registry):
        fm = _make_field_map(mappings={"User": "user_name"})
        registry.save_map(fm)

        updated = _make_field_map(mappings={"User": "account_name", "Image": "proc"})
        registry.save_map(updated)

        loaded = registry.get_map("sigma")
        assert loaded.mappings == {"User": "account_name", "Image": "proc"}

    def test_delete_existing(self, registry):
        fm = _make_field_map(
            source="windows-audit",
            mappings={"User": "user_name"},
        )
        registry.save_map(fm)
        assert registry.map_exists("sigma", "windows-audit")

        registry.delete_map("sigma", "windows-audit")
        assert not registry.map_exists("sigma", "windows-audit")

    def test_delete_nonexistent_is_noop(self, registry):
        registry.delete_map("sigma", "nonexistent")

    def test_delete_default(self, registry):
        fm = _make_field_map(mappings={"User": "user_name"})
        registry.save_map(fm)

        registry.delete_map("sigma")
        assert not registry.map_exists("sigma")


# ---------------------------------------------------------------
# List
# ---------------------------------------------------------------


class TestList:
    def test_list_empty(self, registry):
        assert registry.list_maps() == []

    def test_list_multiple(self, registry):
        registry.save_map(_make_field_map(standard="sigma", mappings={"A": "a"}))
        registry.save_map(_make_field_map(standard="sigma", source="win", mappings={"B": "b"}))
        registry.save_map(_make_field_map(standard="ecs", mappings={"C": "c"}))

        results = registry.list_maps()
        assert len(results) == 3

        standards = {r["standard"] for r in results}
        assert standards == {"sigma", "ecs"}

    def test_list_filtered_by_standard(self, registry):
        registry.save_map(_make_field_map(standard="sigma", mappings={"A": "a"}))
        registry.save_map(_make_field_map(standard="ecs", mappings={"B": "b"}))

        sigma_results = registry.list_maps(standard="sigma")
        assert len(sigma_results) == 1
        assert sigma_results[0]["standard"] == "sigma"

    def test_list_includes_metadata(self, registry):
        registry.save_map(
            _make_field_map(
                standard="ecs",
                source="windows-audit",
                mappings={"source.ip": "source_ip", "user.name": "user_name"},
                version="8.11",
            )
        )

        results = registry.list_maps()
        assert len(results) == 1
        entry = results[0]
        assert entry["standard"] == "ecs"
        assert entry["source"] == "windows-audit"
        assert entry["is_default"] is False
        assert entry["version"] == "8.11"
        assert entry["mapping_count"] == 2
        assert entry["updated_at"] is not None


# ---------------------------------------------------------------
# Get Maps For Standard
# ---------------------------------------------------------------


class TestGetMapsForStandard:
    def test_returns_correct_standard(self, registry):
        registry.save_map(_make_field_map(standard="sigma", mappings={"A": "a"}))
        registry.save_map(_make_field_map(standard="sigma", source="win", mappings={"B": "b"}))
        registry.save_map(_make_field_map(standard="ecs", mappings={"C": "c"}))

        sigma_maps = registry.get_maps_for_standard("sigma")
        assert len(sigma_maps) == 2
        assert all(m.standard == "sigma" for m in sigma_maps)

    def test_excludes_other_standards(self, registry):
        registry.save_map(_make_field_map(standard="sigma", mappings={"A": "a"}))
        registry.save_map(_make_field_map(standard="ecs", mappings={"B": "b"}))

        ecs_maps = registry.get_maps_for_standard("ecs")
        assert len(ecs_maps) == 1
        assert ecs_maps[0].standard == "ecs"

    def test_empty_for_unknown_standard(self, registry):
        assert registry.get_maps_for_standard("ocsf") == []


# ---------------------------------------------------------------
# Map Exists
# ---------------------------------------------------------------


class TestMapExists:
    def test_exists(self, registry):
        registry.save_map(_make_field_map(mappings={"A": "a"}))
        assert registry.map_exists("sigma") is True

    def test_not_exists(self, registry):
        assert registry.map_exists("sigma") is False

    def test_exists_source_specific(self, registry):
        registry.save_map(_make_field_map(source="windows-audit", mappings={"A": "a"}))
        assert registry.map_exists("sigma", "windows-audit") is True
        assert registry.map_exists("sigma", "linux-syslog") is False


# ---------------------------------------------------------------
# Seed Defaults
# ---------------------------------------------------------------


class TestSeedDefaults:
    def test_seeds_all_standards(self, registry):
        count = registry.seed_defaults()
        assert count == 4  # sigma, ecs, cim, ocsf

        # Verify each standard has a non-empty default map
        sigma = registry.get_map("sigma")
        assert sigma.standard == "sigma"
        assert len(sigma.mappings) > 0

        ecs = registry.get_map("ecs")
        assert ecs.standard == "ecs"
        assert len(ecs.mappings) > 0

        cim = registry.get_map("cim")
        assert cim.standard == "cim"
        assert len(cim.mappings) > 0

        ocsf = registry.get_map("ocsf")
        assert ocsf.standard == "ocsf"
        assert len(ocsf.mappings) > 0

    def test_non_destructive(self, registry):
        # Seed once
        registry.seed_defaults()

        # Modify sigma default
        custom = _make_field_map(mappings={"Custom": "custom_col"})
        registry.save_map(custom)

        # Seed again — should NOT overwrite
        count = registry.seed_defaults()
        assert count == 0

        loaded = registry.get_map("sigma")
        assert loaded.mappings == {"Custom": "custom_col"}

    def test_overwrite_mode(self, registry):
        # Seed once
        registry.seed_defaults()

        # Modify sigma default
        custom = _make_field_map(mappings={"Custom": "custom_col"})
        registry.save_map(custom)

        # Seed with overwrite — should replace
        count = registry.seed_defaults(overwrite=True)
        assert count == 4  # sigma, ecs, cim, ocsf

        loaded = registry.get_map("sigma")
        assert "Custom" not in loaded.mappings
        assert "User" in loaded.mappings  # original default field

    def test_seeded_sigma_has_expected_fields(self, registry):
        registry.seed_defaults()
        sigma = registry.get_map("sigma")
        assert sigma.mappings["EventID"] == "event_id"
        assert sigma.mappings["CommandLine"] == "command_line"
        assert sigma.mappings["Image"] == "process_name"
        assert sigma.mappings["User"] == "user_name"

    def test_seeded_ecs_has_expected_fields(self, registry):
        registry.seed_defaults()
        ecs = registry.get_map("ecs")
        assert ecs.mappings["source.ip"] == "source_ip"
        assert ecs.mappings["user.name"] == "user_name"
        assert ecs.mappings["process.name"] == "process_name"
        assert ecs.version == "8.11"

    def test_seeded_cim_has_expected_fields(self, registry):
        registry.seed_defaults()
        cim = registry.get_map("cim")
        assert cim.mappings["src_ip"] == "source_ip"
        assert cim.mappings["user"] == "user_name"
        assert cim.mappings["process_name"] == "process_name"


# ---------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------


class TestSingleton:
    def test_get_instance_requires_dir(self):
        FieldMapRegistry.reset_instance()
        with pytest.raises(FieldMapError, match="field_maps_directory is required"):
            FieldMapRegistry.get_instance()
        FieldMapRegistry.reset_instance()

    def test_get_instance_returns_same(self, field_maps_dir):
        FieldMapRegistry.reset_instance()
        try:
            inst1 = FieldMapRegistry.get_instance(
                field_maps_directory=field_maps_dir,
                refresh_interval=0,
            )
            inst2 = FieldMapRegistry.get_instance()
            assert inst1 is inst2
        finally:
            FieldMapRegistry.reset_instance()


# ---------------------------------------------------------------
# Multiple Standards Coexistence
# ---------------------------------------------------------------


class TestMultiStandard:
    def test_same_source_different_standards(self, registry):
        """Same source name under different standards should not conflict."""
        registry.save_map(
            _make_field_map(
                standard="sigma",
                source="windows-audit",
                mappings={"User": "user_name"},
            )
        )
        registry.save_map(
            _make_field_map(
                standard="ecs",
                source="windows-audit",
                mappings={"user.name": "user_name"},
            )
        )

        sigma = registry.get_map("sigma", "windows-audit")
        ecs = registry.get_map("ecs", "windows-audit")
        assert sigma.mappings == {"User": "user_name"}
        assert ecs.mappings == {"user.name": "user_name"}

    def test_default_and_source_coexist(self, registry):
        registry.save_map(
            _make_field_map(
                standard="sigma",
                mappings={"User": "user_name"},
            )
        )
        registry.save_map(
            _make_field_map(
                standard="sigma",
                source="windows-audit",
                mappings={"User": "account_name"},
            )
        )

        default = registry.get_map("sigma")
        specific = registry.get_map("sigma", "windows-audit")
        assert default.mappings["User"] == "user_name"
        assert specific.mappings["User"] == "account_name"
