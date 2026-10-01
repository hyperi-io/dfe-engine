"""Tests for ViewGenerator — ClickHouse view DDL from field maps."""

import pytest

from dfe_engine.fieldmap.models import FieldMap
from dfe_engine.fieldmap.registry import FieldMapRegistry
from dfe_engine.fieldmap.view_generator import ViewGenerator
from dfe_engine.schema.schema_ddl import DDLConfig

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_field_map(
    standard: str = "sigma",
    source: str | None = None,
    mappings: dict[str, str] | None = None,
) -> FieldMap:
    return FieldMap(
        standard=standard,
        source=source,
        mappings=mappings or {},
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


@pytest.fixture
def gen(registry):
    return ViewGenerator(registry)


# ---------------------------------------------------------------
# generate_view
# ---------------------------------------------------------------


class TestGenerateView:
    def test_returns_none_when_no_maps(self, gen: ViewGenerator):
        result = gen.generate_view("sigma", "windows-audit", "windows-audit")
        assert result is None

    def test_generates_view_from_default_map(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(
            _make_field_map(
                standard="sigma",
                mappings={"EventID": "event_id", "User": "user_name"},
            )
        )
        ddl = gen.generate_view("sigma", "windows-audit", "windows-audit")
        assert ddl is not None
        assert "CREATE OR REPLACE VIEW" in ddl
        assert "windows-audit_sigma" in ddl
        assert "`event_id` AS `EventID`" in ddl
        assert "`user_name` AS `User`" in ddl
        assert "FROM `{db}`.`windows-audit`" in ddl

    def test_generates_view_from_source_map(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(
            _make_field_map(
                standard="ecs",
                source="syslog",
                mappings={"source.ip": "src_ip"},
            )
        )
        ddl = gen.generate_view("ecs", "syslog", "syslog")
        assert ddl is not None
        assert "syslog_ecs" in ddl
        assert "`src_ip` AS `source.ip`" in ddl

    def test_source_overrides_default(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(
            _make_field_map(
                standard="sigma",
                mappings={"EventID": "event_id", "User": "user_name"},
            )
        )
        registry.save_map(
            _make_field_map(
                standard="sigma",
                source="crowdstrike",
                mappings={"EventID": "cs_event_id"},
            )
        )
        ddl = gen.generate_view("sigma", "crowdstrike", "crowdstrike")
        assert ddl is not None
        # Source override
        assert "`cs_event_id` AS `EventID`" in ddl
        # Default preserved
        assert "`user_name` AS `User`" in ddl

    def test_custom_config(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(_make_field_map(standard="sigma", mappings={"X": "x"}))
        cfg = DDLConfig(db="analytics")
        ddl = gen.generate_view("sigma", "test-src", "test_table", cfg)
        assert ddl is not None
        assert "`analytics`.`test_table_sigma`" in ddl
        assert "FROM `analytics`.`test_table`" in ddl

    def test_view_includes_star(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(_make_field_map(standard="sigma", mappings={"A": "a"}))
        ddl = gen.generate_view("sigma", "src", "tbl")
        assert ddl is not None
        assert "*" in ddl


# ---------------------------------------------------------------
# generate_views_for_source
# ---------------------------------------------------------------


class TestGenerateViewsForSource:
    def test_empty_when_no_maps(self, gen: ViewGenerator):
        views = gen.generate_views_for_source("windows-audit", "windows-audit")
        assert views == {}

    def test_generates_for_all_discovered_standards(
        self, gen: ViewGenerator, registry: FieldMapRegistry
    ):
        registry.save_map(_make_field_map(standard="sigma", mappings={"EventID": "event_id"}))
        registry.save_map(_make_field_map(standard="ecs", mappings={"source.ip": "source_ip"}))
        views = gen.generate_views_for_source("windows-audit", "windows-audit")
        assert "sigma" in views
        assert "ecs" in views
        assert "windows-audit_sigma" in views["sigma"]
        assert "windows-audit_ecs" in views["ecs"]

    def test_includes_source_specific_standard(
        self, gen: ViewGenerator, registry: FieldMapRegistry
    ):
        # Only a source-specific map, no default
        registry.save_map(
            _make_field_map(
                standard="cim",
                source="syslog",
                mappings={"src_ip": "source_ip"},
            )
        )
        views = gen.generate_views_for_source("syslog", "syslog")
        assert "cim" in views

    def test_excludes_other_source_maps(self, gen: ViewGenerator, registry: FieldMapRegistry):
        # Map for a different source — should not appear
        registry.save_map(
            _make_field_map(
                standard="cim",
                source="crowdstrike",
                mappings={"src_ip": "source_ip"},
            )
        )
        views = gen.generate_views_for_source("syslog", "syslog")
        assert "cim" not in views

    def test_explicit_standards_list(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(_make_field_map(standard="sigma", mappings={"A": "a"}))
        registry.save_map(_make_field_map(standard="ecs", mappings={"B": "b"}))
        views = gen.generate_views_for_source("src", "tbl", standards=["sigma"])
        assert "sigma" in views
        assert "ecs" not in views

    def test_standards_list_with_no_maps(self, gen: ViewGenerator):
        views = gen.generate_views_for_source("src", "tbl", standards=["sigma", "ecs"])
        assert views == {}

    def test_custom_config_passed_through(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(_make_field_map(standard="sigma", mappings={"X": "x"}))
        cfg = DDLConfig(db="mydb")
        views = gen.generate_views_for_source("src", "tbl", config=cfg)
        assert "`mydb`.`tbl_sigma`" in views["sigma"]


# ---------------------------------------------------------------
# _discover_standards
# ---------------------------------------------------------------


class TestDiscoverStandards:
    def test_empty_registry(self, gen: ViewGenerator):
        result = gen._discover_standards("any_source")
        assert result == []

    def test_discovers_defaults_and_source_maps(
        self, gen: ViewGenerator, registry: FieldMapRegistry
    ):
        registry.save_map(_make_field_map(standard="sigma", mappings={"A": "a"}))
        registry.save_map(_make_field_map(standard="ecs", source="syslog", mappings={"B": "b"}))
        registry.save_map(
            _make_field_map(standard="cim", source="crowdstrike", mappings={"C": "c"})
        )
        result = gen._discover_standards("syslog")
        # sigma (default), ecs (source-specific for syslog) — NOT cim (different source)
        assert result == ["ecs", "sigma"]

    def test_sorted_output(self, gen: ViewGenerator, registry: FieldMapRegistry):
        for std in ["cim", "sigma", "ecs"]:
            registry.save_map(_make_field_map(standard=std, mappings={"X": "x"}))
        result = gen._discover_standards("any")
        assert result == ["cim", "ecs", "sigma"]


# ---------------------------------------------------------------
# _resolve_map
# ---------------------------------------------------------------


class TestResolveMap:
    def test_returns_empty_when_no_maps(self, gen: ViewGenerator):
        result = gen._resolve_map("sigma", "windows-audit")
        assert result == {}

    def test_default_only(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(
            _make_field_map(
                standard="sigma",
                mappings={"EventID": "event_id", "User": "user_name"},
            )
        )
        result = gen._resolve_map("sigma", "windows-audit")
        assert result == {"EventID": "event_id", "User": "user_name"}

    def test_source_overrides_default(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(
            _make_field_map(
                standard="sigma",
                mappings={"EventID": "event_id", "User": "user_name"},
            )
        )
        registry.save_map(
            _make_field_map(
                standard="sigma",
                source="crowdstrike",
                mappings={"EventID": "cs_event_id"},
            )
        )
        result = gen._resolve_map("sigma", "crowdstrike")
        assert result == {"EventID": "cs_event_id", "User": "user_name"}

    def test_source_map_only(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(
            _make_field_map(
                standard="ecs",
                source="syslog",
                mappings={"source.ip": "src_ip"},
            )
        )
        result = gen._resolve_map("ecs", "syslog")
        assert result == {"source.ip": "src_ip"}

    def test_none_source_name(self, gen: ViewGenerator, registry: FieldMapRegistry):
        registry.save_map(_make_field_map(standard="sigma", mappings={"X": "x"}))
        result = gen._resolve_map("sigma", None)
        assert result == {"X": "x"}
