#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_source_views_ch.py
#  Purpose:      A source deploy's naming-standard views are applied to a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Deploy a source's sigma, ECS and CIM views against a live server, from the shipped maps.

ClickHouse refuses a whole view (code 47) when any one of its columns is missing,
and the default maps name far more columns than any one table holds. These run
the build and deploy functions the source deploy endpoint runs, with the maps the
installed dfe-schemas seeds, and then ask the server what it created.
"""

import uuid
from pathlib import Path

import dfe_schemas
import pytest

from dfe_engine.fieldmap.registry import FieldMapRegistry
from dfe_engine.schema.applier import SchemaApplier
from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.schema.schema_loader import resolve_registry_path
from dfe_engine.settings import DFESettings
from dfe_engine.source.deployment import (
    deploy_statements_for_build,
    execute_ddl_statements,
    run_source_build,
)
from dfe_engine.source.models import Source
from dfe_engine.source.type_registry import TypeRegistry
from dfe_engine.yaml_utils import yaml_dump, yaml_load

pytestmark = pytest.mark.integration

SHIPPED_SCHEMAS = Path(dfe_schemas.__file__).parent / "data"

# Some targets of the shipped sigma, ECS and CIM maps, and none of the others.
CARRIED = [
    "event_code",
    "event_action",
    "process_executable",
    "process_command_line",
    "process_name",
    "file_path",
    "host_name",
    "user_name",
    "source_ip",
]


@pytest.fixture
def source_database(ch_client):
    """A throwaway database for one deploy, dropped afterwards."""
    db = f"dfe_source_views_{uuid.uuid4().hex[:8]}"
    resolver = EngineResolver(client=ch_client)
    SchemaApplier(ch_client, resolver).ensure_database(db)
    try:
        yield db
    finally:
        try:
            on_cluster = resolver.resolve(parse_engine("MergeTree"), db).on_cluster
            ch_client.command(f"DROP DATABASE IF EXISTS `{db}`{on_cluster} SYNC")
        except Exception:
            pass


@pytest.fixture
def seeded_maps(tmp_path):
    """A field-map registry holding exactly what the installed dfe-schemas seeds."""
    registry = FieldMapRegistry(tmp_path / "field-maps", writable=True, refresh_interval=0)
    assert registry.seed_defaults() > 0, "the installed dfe-schemas seeds no field maps"
    try:
        yield registry
    finally:
        registry.close()


def _shipped_map(standard: str) -> dict[str, str]:
    return yaml_load(resolve_registry_path("field-maps") / standard / "_default.yaml")["mappings"]


def _deploy(manager_client, db: str, source: Source, schemas_dir: Path, maps) -> dict[str, str]:
    """Build and apply *source* the way the deploy endpoint does; return the view DDLs."""
    result = run_source_build(
        source,
        version_id=source.runtime_version_id(),
        schemas_base_dir=schemas_dir,
        resolver=EngineResolver(client=manager_client),
        settings=DFESettings(env="dev"),
        field_map_registry=maps,
    )
    assert not result.validation_errors, result.validation_errors
    # The builder here only renders ALTERs for an existing table; the views come from result.
    statements, _ = deploy_statements_for_build(
        SchemaBuilderV2(resolver=EngineResolver(client=manager_client)),
        source,
        source.runtime_version_id(),
        result,
        db=db,
        ch_client=manager_client,
    )
    executed, failed = execute_ddl_statements(manager_client, statements)
    assert failed == [], failed
    assert len(executed) == len(statements)
    return result.view_ddls


def _tables(ch_client, db: str) -> dict[str, str]:
    rows = ch_client.query(
        "SELECT name, engine FROM system.tables WHERE database = {db:String}",
        parameters={"db": db},
    ).result_rows
    return {str(name): str(engine) for name, engine in rows}


def _columns(ch_client, db: str, table: str) -> set[str]:
    rows = ch_client.query(
        "SELECT name FROM system.columns WHERE database = {db:String} AND table = {tbl:String}",
        parameters={"db": db, "tbl": table},
    ).result_rows
    return {str(row[0]) for row in rows}


def test_shipped_maps_deploy_views_over_only_the_columns_a_table_carries(
    tmp_path, ch_client, manager_client, source_database, seeded_maps
):
    """Each view is created, carries every usable alias and none of the dropped ones."""
    yaml_dump(
        {"columns": [{"name": name, "type": "string"} for name in CARRIED]},
        tmp_path / "carried.yaml",
    )
    source = Source.model_validate(
        {
            "source": "winlike",
            "match": {"field": "tags.collector.type", "value": "winlike"},
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {"ttl_days": 30, "engine": "", "meta_schema": "carried.yaml"},
            "views": [
                {"standard": "sigma"},
                {"standard": "ecs"},
                # An operator's inline mapping to a column the table lacks.
                {"standard": "cim", "custom_mappings": {"vendor_product": "no_such_column"}},
            ],
        }
    )

    view_ddls = _deploy(manager_client, source_database, source, tmp_path, seeded_maps)

    assert set(view_ddls) == {"sigma", "ecs", "cim"}
    tables = _tables(ch_client, source_database)
    table_columns = _columns(ch_client, source_database, "winlike")
    for standard in ("sigma", "ecs", "cim"):
        view = f"winlike_{standard}"
        assert tables.get(view) == "View", f"{view} was not created: {tables}"
        shipped = _shipped_map(standard)
        if standard == "cim":
            shipped = {**shipped, "vendor_product": "no_such_column"}
        usable = {f for f, col in shipped.items() if col in table_columns}
        dropped = {f for f, col in shipped.items() if col not in table_columns}
        assert usable, f"no shipped {standard} target is in {sorted(table_columns)}"
        assert dropped, f"every shipped {standard} target is carried, so nothing was proved"
        view_columns = _columns(ch_client, source_database, view)
        assert usable <= view_columns, sorted(usable - view_columns)
        # A dropped alias that is also a physical column comes through SELECT *.
        assert not (dropped - table_columns) & view_columns

    # The views answer with the row's values under the standard's names.
    ch_client.command(
        f"INSERT INTO `{source_database}`.`winlike` (event_code, process_executable, file_path, "
        "host_name, process_name) VALUES ('4688', 'C:\\\\cmd.exe', 'C:\\\\a.txt', 'ws01', 'cmd.exe')"
    )
    sigma = ch_client.query(
        f"SELECT EventID, Image, TargetFilename FROM `{source_database}`.`winlike_sigma`"
    ).result_rows
    ecs = ch_client.query(f"SELECT `host.name` FROM `{source_database}`.`winlike_ecs`").result_rows
    cim = ch_client.query(
        f"SELECT process_name, file_path FROM `{source_database}`.`winlike_cim`"
    ).result_rows
    assert sigma == [("4688", "C:\\cmd.exe", "C:\\a.txt")]
    assert ecs == [("ws01",)]
    assert cim == [("cmd.exe", "C:\\a.txt")]


def test_dfe_alerts_sigma_view_is_skipped_and_its_deploy_applies(
    ch_client, manager_client, source_database, seeded_maps
):
    """The shipped dfe-alerts source declares a sigma view over a table holding no target."""
    definition = yaml_load(SHIPPED_SCHEMAS / "sources" / "dfe-alerts.yaml")
    source = Source.model_validate({**definition, "source": "dfe-alerts"})
    assert source.view_for("sigma") is not None, "dfe-alerts no longer declares a sigma view"

    view_ddls = _deploy(manager_client, source_database, source, SHIPPED_SCHEMAS, seeded_maps)

    assert "sigma" not in view_ddls
    tables = _tables(ch_client, source_database)
    assert "dfe-alerts" in tables
    assert "dfe-alerts_sigma" not in tables

    # Without the filter the same deploy carries the whole default map, which the
    # server refuses: this is the statement the filter keeps out.
    unfiltered = DDLGenerator(TypeRegistry.default()).generate_view(
        "dfe-alerts", _shipped_map("sigma"), "sigma", DDLConfig(db=source_database)
    )
    with pytest.raises(Exception, match=r"(?s)UNKNOWN_IDENTIFIER|Code: 47"):
        ch_client.command(unfiltered)
