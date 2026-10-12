#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_json_merge_cap_ch.py
#  Purpose:      JSON tables carry the merge cap on a real ClickHouse, created or reconciled
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A JSON table's merge cap, read back from the server that holds the table.

Every assertion reads ``system.tables.engine_full``: what the generator meant is
not evidence, what the server reports is. A table created before the cap existed
is made by creating it and then ``RESET SETTING``, which leaves exactly the shape a
deployment upgraded onto this release has.
"""

import uuid

import pytest
from dfe_schemas.clickhouse import Topology
from dfe_schemas.deploy_defaults import JSON_TABLE_SETTINGS
from dfe_schemas.manifest import load_manifest
from dfe_schemas.render import Renderer

from dfe_engine.api.v1 import sources as sources_module
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema.applier import SchemaApplier
from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
from dfe_engine.schema.manifest_applier import ManifestApplier
from dfe_engine.schema.phase import apply_plan
from dfe_engine.schema.plan import build_plan, core_schemas_root
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.settings import (
    APISettings,
    ClickHouseResilienceSettings,
    ClickHouseSettings,
    DFESettings,
)
from dfe_engine.source.deployment import SettingsChange, deploy_statements_for_build
from dfe_engine.source.models import SchemaColumn, Source
from dfe_engine.source.type_registry import TypeRegistry

pytestmark = pytest.mark.integration

MERGE_CAP = "merge_max_dynamic_subcolumns_in_wide_part"
CAPPED = f"{MERGE_CAP} = {JSON_TABLE_SETTINGS[MERGE_CAP]}"
JSON_TABLES = ("main", "detection", "otel_logs")


@pytest.fixture
def database(ch_client):
    """A throwaway database, dropped afterwards with the ON CLUSTER it was made with."""
    name = f"dfe_json_cap_{uuid.uuid4().hex[:8]}"
    resolver = EngineResolver(client=ch_client)
    SchemaApplier(ch_client, resolver).ensure_database(name)
    try:
        yield name
    finally:
        on_cluster = resolver.resolve(parse_engine("MergeTree"), name).on_cluster
        ch_client.command(f"DROP DATABASE IF EXISTS `{name}`{on_cluster} SYNC")


def _engine_full(client, database: str, table: str) -> str:
    rows = client.query(
        "SELECT engine_full FROM system.tables WHERE database = {db:String} AND name = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    assert rows, f"{database}.{table} does not exist"
    return str(rows[0][0])


def _uncap(client, database: str, table: str) -> None:
    """Leave the table as one created before the cap existed."""
    client.command(f"ALTER TABLE `{database}`.`{table}` RESET SETTING {MERGE_CAP}")
    assert MERGE_CAP not in _engine_full(client, database, table)


def _settings_alters(statements: list[str]) -> list[str]:
    return [s for s in statements if "MODIFY SETTING" in s]


# -- source tables: the generator and SchemaApplier.ensure_table ------------


def _source_columns():
    """The header every source landing table carries, _json included."""
    return SchemaLoader.load_profile("timeseries")


def test_a_new_source_table_is_created_with_the_cap(ch_client, database):
    gen = DDLGenerator(TypeRegistry.default(), resolver=EngineResolver(client=ch_client))

    ch_client.command(
        gen.generate_create_table("s3probe", _source_columns(), DDLConfig(db=database))
    )

    assert CAPPED in _engine_full(ch_client, database, "s3probe")


def test_a_source_table_without_the_cap_is_reconciled_once(ch_client, database):
    """The pass every engine start runs over deployed source tables adds it, then leaves it."""
    resolver = EngineResolver(client=ch_client)
    gen = DDLGenerator(TypeRegistry.default(), resolver=resolver)
    cfg = DDLConfig(db=database)
    ch_client.command(gen.generate_create_table("s3probe", _source_columns(), cfg))
    _uncap(ch_client, database, "s3probe")

    first = SchemaApplier(ch_client, resolver)
    change = first.ensure_table(database, "s3probe", _source_columns(), cfg)

    assert CAPPED in _engine_full(ch_client, database, "s3probe")
    assert _settings_alters(first.report.statements) == [
        f"ALTER TABLE `{database}`.`s3probe` MODIFY SETTING {CAPPED}"
    ]
    assert change.action == "altered"
    assert change.settings == (CAPPED,)
    assert f"set {CAPPED}" in change.describe()

    second = SchemaApplier(ch_client, EngineResolver(client=ch_client))
    again = second.ensure_table(database, "s3probe", _source_columns(), cfg)

    assert _settings_alters(second.report.statements) == []
    assert again.action == "unchanged"
    assert CAPPED in _engine_full(ch_client, database, "s3probe")


def test_a_source_table_with_no_json_column_is_never_altered(ch_client, database):
    columns = [
        SchemaColumn(name="_timestamp_load", type="timestamp", order=0),
        SchemaColumn(name="message", type="text"),
    ]
    cfg = DDLConfig(db=database)
    applier = SchemaApplier(ch_client, EngineResolver(client=ch_client))
    applier.ensure_table(database, "plain", columns, cfg)

    applier.ensure_table(database, "plain", columns, cfg)

    assert MERGE_CAP not in _engine_full(ch_client, database, "plain")
    assert _settings_alters(applier.report.statements) == []


def test_a_cluster_pin_puts_on_cluster_on_the_settings_alter(ch_client, database):
    """The same ON CLUSTER the TTL reconcile takes, so no replica keeps the old setting."""
    resolver = EngineResolver(client=ch_client)
    gen = DDLGenerator(TypeRegistry.default(), resolver=resolver)
    ch_client.command(
        gen.generate_create_table("s3probe", _source_columns(), DDLConfig(db=database))
    )
    _uncap(ch_client, database, "s3probe")

    planning = SchemaApplier(ch_client, resolver, dry_run=True)
    planning.ensure_table(
        database, "s3probe", _source_columns(), DDLConfig(db=database, cluster="dfe_cluster")
    )

    assert _settings_alters(planning.report.statements) == [
        f"ALTER TABLE `{database}`.`s3probe` ON CLUSTER dfe_cluster MODIFY SETTING {CAPPED}"
    ]


# -- source redeploy: plan_deploy_statements -------------------------------


def _redeploy_statements(client, database: str, source: Source) -> tuple[list[str], bool]:
    """The statements a deploy of *source* plans against the server as it is now.

    *client* is the engine's manager wrapper, which is what the deploy endpoint hands
    the planner. The flag is whether the planner found the table.
    """
    builder = SchemaBuilderV2(resolver=EngineResolver(client=client))
    result = builder.build(source)
    assert not result.validation_errors, result.validation_errors
    statements, table_exists = deploy_statements_for_build(
        builder, source, source.runtime_version_id(), result, db=database, ch_client=client
    )
    return statements, table_exists


def _source(name: str) -> Source:
    return Source.model_validate(
        {
            "source": name,
            "match": {"field": "tags.collector.type", "value": name},
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {"ttl_days": 90, "engine": ""},
        }
    )


def test_a_redeploy_of_a_source_table_without_the_cap_sets_it_once(
    ch_client, manager_client, database
):
    """Running the planned statements adds the cap, and the next redeploy plans none."""
    source = _source("redeploy-probe")
    first, found = _redeploy_statements(manager_client, database, source)
    assert not found
    for statement in first:
        ch_client.command(statement)
    assert CAPPED in _engine_full(ch_client, database, source.table_name)
    _uncap(ch_client, database, source.table_name)

    planned, found = _redeploy_statements(manager_client, database, source)

    assert found
    assert _settings_alters(planned) == [
        f"ALTER TABLE `{database}`.`{source.table_name}` MODIFY SETTING {CAPPED}"
    ]
    for statement in planned:
        ch_client.command(statement)
    assert CAPPED in _engine_full(ch_client, database, source.table_name)
    again, found = _redeploy_statements(manager_client, database, source)
    assert found
    assert _settings_alters(again) == []


def test_a_redeploy_of_a_source_table_with_the_cap_plans_no_settings_alter(
    ch_client, manager_client, database
):
    source = _source("redeploy-capped")
    first, _ = _redeploy_statements(manager_client, database, source)
    for statement in first:
        ch_client.command(statement)

    planned, found = _redeploy_statements(manager_client, database, source)

    assert found
    assert _settings_alters(planned) == []


def test_a_dry_run_reports_the_settings_move_a_redeploy_would_make(
    ch_client, manager_client, ch_params, database, tmp_path
):
    """The dry run names the same ALTER the plan emits, and none once the table is at the cap."""
    source = _source("dry-run-probe")
    first, _ = _redeploy_statements(manager_client, database, source)
    for statement in first:
        ch_client.command(statement)
    _uncap(ch_client, database, source.table_name)
    deployed = source.model_copy(update={"deployed_version": source.current})
    builder = SchemaBuilderV2(resolver=EngineResolver(client=ch_client))
    result = builder.build(source)
    settings = DFESettings(
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(
            host=ch_params["host"],
            port=ch_params["port"],
            username=ch_params["username"],
            password=ch_params["password"],
            secure=False,
            data_database=database,
            bootstrap_tables=False,
            resilience=ClickHouseResilienceSettings(budget_seconds=5.0),
        ),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )

    def dry_run():
        return sources_module._dry_run_table_moves(
            builder, deployed, deployed.current, result, settings
        )

    ClickHouseManager.reset_instance()
    try:
        ttl_change, settings_change, error = dry_run()
        planned, _ = _redeploy_statements(manager_client, database, source)
        for statement in planned:
            ch_client.command(statement)
        _, settings_after, error_after = dry_run()
    finally:
        ClickHouseManager.reset_instance()

    assert error is None
    assert ttl_change is None
    assert settings_change == SettingsChange(
        assignments=[CAPPED],
        statement=f"ALTER TABLE `{database}`.`{source.table_name}` MODIFY SETTING {CAPPED}",
    )
    assert _settings_alters(planned) == [settings_change.statement]
    assert error_after is None
    assert settings_after is None


# -- core tables: the manifest render and ManifestApplier -------------------


@pytest.fixture
def plan(database):
    from dfe_engine.settings import load_settings

    settings = load_settings().model_copy(deep=True)
    settings.clickhouse.data_database = database
    return build_plan(settings=settings)


def test_the_manifest_json_tables_are_created_with_the_cap(ch_client, database, plan):
    report = apply_plan(ch_client, plan)

    assert not report.refused, [outcome.describe() for outcome in report.refused]
    for table in JSON_TABLES:
        assert CAPPED in _engine_full(ch_client, database, table), table
    assert MERGE_CAP not in _engine_full(ch_client, database, "alert_state")


def test_a_manifest_table_without_the_cap_is_reconciled_once(ch_client, database, plan):
    """The boot phase adds the cap to a table created by an older release, then leaves it."""
    apply_plan(ch_client, plan)
    _uncap(ch_client, database, "main")

    second = apply_plan(ch_client, plan)

    assert CAPPED in _engine_full(ch_client, database, "main")
    assert _settings_alters(second.statements) == [
        f"ALTER TABLE `{database}`.`main` MODIFY SETTING {CAPPED}"
    ]
    main = next(outcome for outcome in second.outcomes if outcome.id == "data.main")
    assert main.action == "altered"
    assert main.settings == (CAPPED,)
    assert f"set {CAPPED}" in main.describe()

    third = apply_plan(ch_client, plan)

    assert _settings_alters(third.statements) == []
    assert not [o.describe() for o in third.outcomes if o.action in ("created", "altered")]


def test_a_cluster_render_puts_on_cluster_on_the_manifest_settings_alter(ch_client, database):
    """The ALTER takes the ON CLUSTER the rendered CREATE carries, as the TTL reconcile does."""
    manifest = load_manifest(root=core_schemas_root())
    main = next(obj for obj in manifest.objects if obj.id == "data.main")
    single = Renderer(manifest, data_database=database, default_ttl_days=90)
    ch_client.command(single.render(main).statements[0])
    _uncap(ch_client, database, "main")

    on_cluster = Renderer(
        manifest,
        topology=Topology.REPLICATED_ON_CLUSTER,
        data_database=database,
        default_ttl_days=90,
        cluster="dfe_cluster",
    ).render(main)
    planning = ManifestApplier(
        ch_client,
        ledger=None,
        schemas_version="test",
        engine_version="test",
        topology=Topology.REPLICATED_ON_CLUSTER.value,
        dry_run=True,
    )
    planning.apply(on_cluster)

    assert _settings_alters(planning.report.statements) == [
        f"ALTER TABLE `{database}`.`main` ON CLUSTER dfe_cluster MODIFY SETTING {CAPPED}"
    ]
