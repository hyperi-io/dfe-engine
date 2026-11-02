#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_platform_ddl_ch.py
#  Purpose:      The engine's own schema apply takes the one DDL shape on real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The engine's schema apply, run against real servers and read back from each.

Every DFE table is an argumentless ``Replicated*`` engine inside a Replicated
database, with no ``ON CLUSTER``, and dfe-schemas' platform probe picks the CREATE
DATABASE and a JSON table's platform-only settings. Proven here against:

* a single node running Keeper embedded, the shape a single-node deployment has
* two replicas sharing one Keeper, where the DDL is issued on one and must reach
  the other, a row written on one must read back on the other, and the roles the
  apply creates must exist on both
* the same pair with replica 2 down, where DDL must land on replica 1 at once and
  reach replica 2 when it returns
* the same pair keeping SQL users and roles locally, which the engine refuses
* a stock server with no Keeper, which the engine refuses with the reason

A Cloud server is not stood up. The Cloud direction of the settings realign runs
with the platform pinned, which is the cascade's override layer.
"""

import time
import uuid

import pytest
from dfe_schemas.clickhouse import (
    Platform,
    PlatformProbe,
    ReplicationNotConfiguredError,
    create_database,
)
from dfe_schemas.render import ddl_violations

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema import phase
from dfe_engine.schema.applier import SchemaApplier
from dfe_engine.schema.phase import apply_plan, run_bootstrap
from dfe_engine.schema.plan import DatabaseEngineError, build_plan
from dfe_engine.schema.schema_ddl import DDLConfig
from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.settings import load_settings

pytestmark = [pytest.mark.integration, pytest.mark.own_clickhouse]

WRITE_MARKS = "write_marks_for_substreams_in_compact_parts"
JSON_TABLES = ("main", "detection", "otel_logs")
META_DATABASE = "dfe_meta"
# Engines that hold no rows of their own: a view stores nothing, every DFE
# materialised view writes TO a replicated table rather than an inner one, and a
# Merge table reads the tables it names.
NO_DATA_ENGINES = ("View", "MaterializedView", "Merge")


def _settings(params: dict, database: str):
    settings = load_settings().model_copy(deep=True)
    settings.clickhouse.host = params["host"]
    settings.clickhouse.port = params["port"]
    settings.clickhouse.username = params["username"]
    settings.clickhouse.password = params["password"]
    settings.clickhouse.secure = params["secure"]
    settings.clickhouse.data_database = database
    settings.clickhouse.bootstrap_tables = True
    settings.kafka.bootstrap_topics = False
    return settings


def _database_engines(client, databases: tuple[str, ...]) -> list[tuple[str, str]]:
    rows = client.query(
        "SELECT name, engine FROM system.databases WHERE name IN {dbs:Array(String)} ORDER BY name",
        parameters={"dbs": list(databases)},
    ).result_rows
    return [(str(name), str(engine)) for name, engine in rows]


def _not_replicated(client, databases: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """The coordinator's check, verbatim: every table outside the Replicated engines."""
    rows = client.query(
        "SELECT database, name, engine FROM system.tables WHERE database IN {dbs:Array(String)} "
        "AND engine NOT LIKE 'Replicated%' AND NOT is_temporary ORDER BY database, name",
        parameters={"dbs": list(databases)},
    ).result_rows
    return [(str(db), str(name), str(engine)) for db, name, engine in rows]


def _table_count(client, databases: tuple[str, ...]) -> int:
    rows = client.query(
        "SELECT count() FROM system.tables WHERE database IN {dbs:Array(String)}",
        parameters={"dbs": list(databases)},
    ).result_rows
    return int(rows[0][0])


def _engine_full(client, database: str, table: str) -> str:
    rows = client.query(
        "SELECT engine_full FROM system.tables WHERE database = {db:String} AND name = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    assert rows, f"{database}.{table} does not exist"
    return str(rows[0][0])


def _outcome(report, object_id: str):
    return next(outcome for outcome in report.outcomes if outcome.id == object_id)


@pytest.fixture
def data_database():
    return f"dfe_platform_{uuid.uuid4().hex[:8]}"


def test_a_single_keeper_node_takes_the_engine_apply_in_the_one_shape(
    ch_client, ch_params, data_database
):
    databases = (data_database, META_DATABASE)
    plan = build_plan(settings=_settings(ch_params, data_database), client=ch_client)

    report = apply_plan(ch_client, plan)

    assert plan.platform == Platform.SELF_HOSTED.value
    assert not report.refused, [outcome.describe() for outcome in report.refused]
    assert _database_engines(ch_client, databases) == [
        (name, "Replicated") for name in sorted(databases)
    ]
    plain = [row for row in _not_replicated(ch_client, databases) if row[2] not in NO_DATA_ENGINES]
    assert plain == [], f"tables that would split across replicas: {plain}"
    for table in JSON_TABLES:
        assert f"{WRITE_MARKS} = 0" in _engine_full(ch_client, data_database, table), table
    assert WRITE_MARKS not in _engine_full(ch_client, data_database, "alert_state")
    problems = [
        problem
        for rendered in plan.tables()
        for statement in rendered.statements
        for problem in ddl_violations(statement)
    ]
    assert problems == []


def test_the_manifest_realign_sets_and_resets_the_platform_setting(
    ch_client, ch_params, data_database
):
    """Self-hosted sets it where a table lacks it, Cloud resets it where a table carries it."""
    settings = _settings(ch_params, data_database)
    self_hosted = build_plan(settings=settings, client=ch_client)
    cloud = build_plan(settings=settings, client=ch_client, platform=Platform.CLOUD)
    apply_plan(ch_client, self_hosted)
    ch_client.command(f"ALTER TABLE `{data_database}`.`main` RESET SETTING {WRITE_MARKS}")
    assert WRITE_MARKS not in _engine_full(ch_client, data_database, "main")

    added = apply_plan(ch_client, self_hosted)

    assert f"{WRITE_MARKS} = 0" in _engine_full(ch_client, data_database, "main")
    assert _outcome(added, "data.main").settings == (f"{WRITE_MARKS} = 0",)

    reset = apply_plan(ch_client, cloud)

    assert cloud.platform == Platform.CLOUD.value
    assert WRITE_MARKS not in _engine_full(ch_client, data_database, "main")
    for table in JSON_TABLES:
        assert WRITE_MARKS not in _engine_full(ch_client, data_database, table), table
    assert _outcome(reset, "data.main").settings == (f"reset {WRITE_MARKS}",)
    assert f"ALTER TABLE `{data_database}`.`main` RESET SETTING {WRITE_MARKS}" in reset.statements

    settled = apply_plan(ch_client, cloud)

    moves = [s for s in settled.statements if "MODIFY SETTING" in s or "RESET SETTING" in s]
    assert moves == [], moves


def test_the_source_table_realign_sets_and_resets_the_platform_setting(
    ch_client, clickhouse_test_database
):
    database = clickhouse_test_database
    columns = SchemaLoader.load_profile("timeseries")
    cfg = DDLConfig(db=database)

    SchemaApplier(ch_client, Platform.SELF_HOSTED).ensure_table(database, "probe", columns, cfg)
    assert f"{WRITE_MARKS} = 0" in _engine_full(ch_client, database, "probe")

    to_cloud = SchemaApplier(ch_client, Platform.CLOUD)
    moved = to_cloud.ensure_table(database, "probe", columns, cfg)

    assert WRITE_MARKS not in _engine_full(ch_client, database, "probe")
    assert moved.action == "altered"
    assert moved.settings == (f"reset {WRITE_MARKS}",)
    assert moved.describe().endswith(f": reset {WRITE_MARKS}")

    back = SchemaApplier(ch_client, Platform.SELF_HOSTED).ensure_table(
        database, "probe", columns, cfg
    )

    assert f"{WRITE_MARKS} = 0" in _engine_full(ch_client, database, "probe")
    assert back.settings == (f"{WRITE_MARKS} = 0",)


def test_a_keeperless_server_is_refused_at_startup_with_the_reason(keeperless_ch_params):
    import clickhouse_connect

    client = clickhouse_connect.get_client(**keeperless_ch_params)
    settings = _settings(keeperless_ch_params, f"dfe_platform_{uuid.uuid4().hex[:8]}")
    assert PlatformProbe(client).platform is Platform.SELF_HOSTED

    with pytest.raises(ReplicationNotConfiguredError) as refused:
        build_plan(settings=settings, client=client)

    message = str(refused.value)
    assert "is missing Keeper" in message
    assert "the {cluster} macro" in message
    assert "docs/clickhouse-platform.md" in message

    ClickHouseManager.reset_instance()
    try:
        state = run_bootstrap(settings=settings, wait_seconds=5.0)
    finally:
        ClickHouseManager.reset_instance()
        phase._set_state(phase.SchemaBootstrapState())

    assert state.state == phase.STATE_FAILED
    assert not state.ready
    assert "is missing Keeper" in state.error
    assert "docs/clickhouse-platform.md" in state.error
    assert state.transient is False
    tables = client.query("SELECT count() FROM system.databases WHERE name = 'dfe_meta'")
    assert tables.result_rows[0][0] == 0, "a refused server was written to"


def _sync(client, databases: tuple[str, ...], want_tables: int, timeout: float = 60.0) -> None:
    """Wait for a replica's Replicated databases to hold *want_tables*, failing past *timeout*."""
    deadline = time.monotonic() + timeout
    while True:
        for database in databases:
            client.command(f"SYSTEM SYNC DATABASE REPLICA `{database}`")
        if _table_count(client, databases) >= want_tables:
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"replica holds {_table_count(client, databases)}/{want_tables} tables")
        time.sleep(1.0)


def test_two_replicas_take_the_engine_apply_from_one_of_them(two_replicas, data_database):
    """The coordinator's proof: DDL on replica 1, zero non-replicated tables on either."""
    import clickhouse_connect

    r1 = clickhouse_connect.get_client(**two_replicas.first)
    r2 = clickhouse_connect.get_client(**two_replicas.second)
    databases = (data_database, META_DATABASE)

    plan = build_plan(settings=_settings(two_replicas.first, data_database), client=r1)
    report = apply_plan(r1, plan)
    assert not report.refused, [outcome.describe() for outcome in report.refused]
    on_cluster = [s for s in report.statements if "ON CLUSTER" in s]
    assert all(s.startswith("CREATE DATABASE") for s in on_cluster), on_cluster
    assert len(on_cluster) == 2, on_cluster

    tables = _table_count(r1, databases)
    _sync(r2, databases, tables)
    for label, client in (("replica 1", r1), ("replica 2", r2)):
        engines = _database_engines(client, databases)
        rows = _not_replicated(client, databases)
        plain = [row for row in rows if row[2] not in NO_DATA_ENGINES]
        print(f"{label} databases: {engines}")
        print(f"{label} tables: {_table_count(client, databases)}")
        print(f"{label} not Replicated*: {len(rows)} rows, {len(plain)} outside {NO_DATA_ENGINES}")
        for row in rows:
            print(f"{label}   {row}")
        assert engines == [(name, "Replicated") for name in sorted(databases)], label
        assert plain == [], f"{label}: {plain}"
    assert _table_count(r2, databases) == tables

    marker = f"two-replica-{uuid.uuid4().hex[:8]}"
    r1.command(
        f"INSERT INTO `{data_database}`.`main` "
        "(_timestamp, _timestamp_received, _org_id, _source, _json, _tags) VALUES "
        f"(now64(3), now64(3), 'probe-org', '{marker}', '{{\"probe\": \"two-replica\"}}', '{{}}')"
    )
    r2.command(f"SYSTEM SYNC REPLICA `{data_database}`.`main`")
    read_back = r2.query(
        f"SELECT count() FROM `{data_database}`.`main` WHERE _source = {{m:String}}",
        parameters={"m": marker},
    ).result_rows[0][0]
    print(f"replica 2 rows for {marker}: {read_back}")
    assert read_back == 1

    roles = {label: _dfe_roles(client) for label, client in (("replica 1", r1), ("replica 2", r2))}
    for label, names in roles.items():
        print(f"{label} dfe roles: {len(names)}")
    assert roles["replica 1"], "the apply created no dfe role"
    assert roles["replica 2"] == roles["replica 1"], "roles did not reach replica 2"


def _dfe_roles(client) -> list[str]:
    rows = client.query("SELECT name FROM system.roles WHERE name LIKE 'dfe%' ORDER BY name")
    return [str(row[0]) for row in rows.result_rows]


def test_ddl_lands_while_a_replica_is_down_and_reaches_it_on_return(two_replicas, data_database):
    """A down replica must not hold the DDL for distributed_ddl_task_timeout and then fail it."""
    import clickhouse_connect

    r1 = clickhouse_connect.get_client(**two_replicas.first)
    r2 = clickhouse_connect.get_client(**two_replicas.second)
    database = data_database
    r1.command(create_database(database, Platform.SELF_HOSTED))
    r2.command(f"SYSTEM SYNC DATABASE REPLICA `{database}`")
    columns = SchemaLoader.load_profile("timeseries")
    cfg = DDLConfig(db=database)
    two_replicas.stop_second()

    # Control: ClickHouse's default mode waits for the down replica, then times out.
    # wait_end_of_query holds the reply, so the timeout arrives as an error and not as
    # a reply cut off part way through.
    control = {
        "distributed_ddl_output_mode": "throw",
        "distributed_ddl_task_timeout": 5,
        "wait_end_of_query": 1,
    }
    with pytest.raises(Exception, match=r"Code: 159|TIMEOUT_EXCEEDED"):
        r1.command(
            f"CREATE TABLE `{database}`.`waits` (k String) ENGINE = ReplicatedMergeTree ORDER BY k",
            settings=control,
        )
    started = time.monotonic()
    change = SchemaApplier(r1, Platform.SELF_HOSTED).ensure_table(database, "lands", columns, cfg)
    elapsed = time.monotonic() - started
    print(f"engine DDL with replica 2 down: {change.action} in {elapsed:.1f}s")

    assert change.action == "created"
    # A replica counts as active until its Keeper session ends, which can take tens of
    # seconds. The failure this guards is the 180 s default timeout and then an error.
    assert elapsed < 120, f"the engine's DDL waited {elapsed:.0f}s on the down replica"
    r2 = clickhouse_connect.get_client(**two_replicas.start_second())
    _sync(r2, (database,), _table_count(r1, (database,)))
    assert _engine_full(r2, database, "lands").startswith("ReplicatedMergeTree")


@pytest.mark.parametrize("two_replicas", [False], indirect=True)
def test_a_cluster_keeping_roles_locally_is_refused_with_the_reason(two_replicas, data_database):
    """DFE creates its roles once, so a cluster's access storage has to be in Keeper."""
    import clickhouse_connect

    r1 = clickhouse_connect.get_client(**two_replicas.first)
    settings = _settings(two_replicas.first, data_database)

    with pytest.raises(ReplicationNotConfiguredError) as refused:
        build_plan(settings=settings, client=r1)

    message = str(refused.value)
    assert "cluster has 2 servers" in message
    assert "local_directory access storage" in message
    assert "docs/clickhouse-platform.md" in message
    assert _database_engines(r1, (data_database, META_DATABASE)) == [], (
        "a refused cluster was written to"
    )


def test_an_atomic_database_from_an_older_install_is_refused_before_any_table(
    ch_client, ch_params, data_database
):
    """Code 36 on the first `{uuid}` table is what this replaces with one sentence."""
    ch_client.command(f"CREATE DATABASE {data_database} ENGINE = Atomic")
    settings = _settings(ch_params, data_database)
    try:
        with pytest.raises(DatabaseEngineError) as refused:
            build_plan(settings=settings, client=ch_client)
        assert f"database {data_database!r} uses the Atomic engine" in str(refused.value)

        ClickHouseManager.reset_instance()
        try:
            state = run_bootstrap(settings=settings, wait_seconds=5.0)
        finally:
            ClickHouseManager.reset_instance()
            phase._set_state(phase.SchemaBootstrapState())

        assert state.state == phase.STATE_FAILED
        assert "uses the Atomic engine" in state.error
        assert _table_count(ch_client, (data_database,)) == 0
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS {data_database} SYNC")
