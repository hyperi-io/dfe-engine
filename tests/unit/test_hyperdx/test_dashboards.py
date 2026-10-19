#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_dashboards.py
#  Purpose:      Guard the shipped HyperDX dashboards and their export path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The dashboards are consumed by another process in another container.

Nothing type-checks a dashboard file: HyperDX's provisioner logs a warning and
skips anything it cannot parse, so a malformed or mis-shaped file shows up as a
dashboard that silently never appears. These tests are the only thing standing
between a bad edit and that outcome, so they assert the file-level contract the
provisioner depends on rather than the content of any one tile.
"""

from __future__ import annotations

import json

import pytest

from dfe_engine.clickhouse.all_sources import ALL_SOURCES
from dfe_engine.hyperdx.dashboards import (
    dashboard_files,
    dashboard_names,
    export_dashboards,
)
from tests.unit.test_hyperdx.dashboards_cases import EPOCH_GUARD_CASES, EpochGuardCase

# Sources the fork seeds on EVERY team (org-connection.ts).
SEEDED_TENANT_SOURCES = {"main", "hunts"}

# The Merge table over `main` and every per-source table, which the engine registers on EVERY team through the fork's /dfe/sources fan-out.
MERGE_SOURCE = ALL_SOURCES

# A dashboard built only from these resolves for a tenant as well as the platform team.
TENANT_SOURCES = SEEDED_TENANT_SOURCES | {MERGE_SOURCE}

# The epoch-0 value a Merge table returns for a DateTime64 column its member table lacks.
EPOCH_ZERO = "toDateTime64(0, 3, 'UTC')"

# Additionally seeded on the platform/admin team only.
PLATFORM_SOURCES = {"otel_logs", "otel_traces", "otel_metrics", "clickhouse_system"}

KNOWN_SOURCES = TENANT_SOURCES | PLATFORM_SOURCES

# The connection name the engine hands a platform caller (api/v1/hyperdx.py); a
# tenant team's connection is named after its org, so this name resolves nowhere
# else and is a second fence on the raw-SQL dashboards.
PLATFORM_CONNECTION = "platform"

# Every raw-SQL tile has to bind to the dashboard's time range, or it silently
# ignores the picker and shows whatever the whole table holds.
TIME_MACROS = ("$__timeFilter", "$__dateTimeFilter", "$__fromTime", "$__dateFilter")

# Tiles whose whole answer is "right now" -- a server's version, its disks, the
# merges in flight. The system tables behind them hold no history to filter.
POINT_IN_TIME_TILES = {
    "now-disk-used",
    "server-info",
    "server-clusters",
    "merges-current",
    "mutations-current",
    "replication-replicas",
    "replication-queue",
    "storage-disks",
    "storage-databases",
    "storage-tables",
    "storage-parts-per-partition",
    "storage-detached",
    "storage-engines",
    "storage-widest",
}

# The Overview's licence numbers: the 7 UTC days to today, whatever the picker shows.
FIXED_WINDOW_TILES = {
    "highest-cores-deployed",
    "highest-node-cores",
    "highest-ingested-bytes",
    "highest-events-per-second",
}
SEVEN_DAYS_TO_TODAY = "day >= toDate(now(), 'UTC') - 6"

# The Kafka transport writes a revoked partition's committed offset as 0, because the
# recorder cannot drop one series.
COMMITTED_OFFSET_METRIC = "rdkafka_topic_partition_committed_offset"

# The key a counter stream is differenced under, as the console's counter_rates query
# keys it (appmgmt/queries.yaml): one attribute set on one pod from one start time.
COUNTER_SERIES_KEY = (
    "cityHash64(Attributes), ResourceAttributes['service.instance.id'], StartTimeUnix"
)


def _dashboards() -> dict[str, dict]:
    return {name: json.loads(text) for name, text in dashboard_files().items()}


def _sql_tiles(dashboard: dict) -> list[dict]:
    return [t for t in dashboard["tiles"] if t["config"].get("configType") == "sql"]


def _throughput_tiles() -> dict[str, dict]:
    """Map each DFE Throughput tile id to its tile."""
    return {tile["id"]: tile for tile in _dashboards()["dfe-throughput.json"]["tiles"]}


def _tile_sources(dashboard: dict) -> set[str]:
    return {
        tile["config"]["source"]
        for tile in dashboard["tiles"]
        if "source" in tile.get("config", {})
    }


def test_dashboards_are_shipped():
    """The package data actually made it into the wheel."""
    assert dashboard_files(), "no dashboard JSON found in the package"


@pytest.mark.parametrize("filename", sorted(dashboard_files()))
def test_dashboard_has_the_template_shape(filename):
    """Template format: a version, a name, and at least one tile.

    ``version`` is what marks the file as a Template rather than a Document; the
    provisioner upserts on ``name``, so an empty or missing name would collide
    every dashboard onto one row.
    """
    dashboard = json.loads(dashboard_files()[filename])

    assert dashboard.get("version"), f"{filename} has no version"
    assert dashboard.get("name"), f"{filename} has no name"
    assert dashboard.get("tiles"), f"{filename} has no tiles"


@pytest.mark.parametrize("filename", sorted(dashboard_files()))
def test_tiles_are_well_formed(filename):
    """Every tile carries the id, geometry and config the schema requires."""
    dashboard = json.loads(dashboard_files()[filename])

    seen_ids = set()
    for tile in dashboard["tiles"]:
        tile_id = tile.get("id")
        assert tile_id, f"{filename}: a tile has no id"
        assert tile_id not in seen_ids, f"{filename}: duplicate tile id {tile_id}"
        seen_ids.add(tile_id)

        for axis in ("x", "y", "w", "h"):
            assert isinstance(tile.get(axis), int), f"{filename}:{tile_id} bad {axis}"

        config = tile.get("config", {})
        assert config.get("name"), f"{filename}:{tile_id} has no title"
        # 24 columns is the dashboard grid width; a tile past it renders clipped.
        assert tile["x"] + tile["w"] <= 24, f"{filename}:{tile_id} overflows the grid"


@pytest.mark.parametrize("filename", sorted(dashboard_files()))
def test_tiles_reference_only_known_sources(filename):
    """A tile naming a source no team holds resolves to nothing and renders dead.

    With ``DASHBOARD_PROVISIONER_REQUIRE_REFS=true`` the whole dashboard is
    skipped instead, so a typo here means a dashboard that never appears on any
    team, with only a warning in the API log to say why.
    """
    dashboard = json.loads(dashboard_files()[filename])
    unknown = _tile_sources(dashboard) - KNOWN_SOURCES

    assert not unknown, f"{filename} references unknown sources: {sorted(unknown)}"


def test_names_are_unique():
    """``name`` is the provisioner's upsert key, so a repeat silently overwrites."""
    names = dashboard_names()

    assert len(names) == len(set(names)), f"duplicate dashboard names: {names}"


def test_the_tenant_dashboard_uses_only_tenant_sources():
    """DFE Throughput is the landing page for every team, tenant included.

    A tenant team holds no otel source, so one otel tile here would take the whole
    dashboard out for tenants under REQUIRE_REFS -- the landing page disappearing
    is the most expensive way for this to break.
    """
    throughput = json.loads(dashboard_files()["dfe-throughput.json"])

    assert _tile_sources(throughput) <= TENANT_SOURCES


def test_every_throughput_tile_names_all_sources():
    tiles = _throughput_tiles()

    assert [
        tile_id for tile_id, tile in tiles.items() if tile["config"].get("source") != MERGE_SOURCE
    ] == []


@pytest.mark.parametrize("case", EPOCH_GUARD_CASES, ids=[case["id"] for case in EPOCH_GUARD_CASES])
def test_throughput_time_maths_skips_epoch_defaults(case: EpochGuardCase):
    config = _throughput_tiles()[case["tile_id"]]["config"]
    guard = f"{case['column']} > {EPOCH_ZERO}"

    assert (config["whereLanguage"], guard in config["where"]) == ("sql", True)


@pytest.mark.parametrize("filename", sorted(dashboard_files()))
def test_raw_sql_tiles_name_a_connection(filename):
    """A raw-SQL tile without a connection has nothing to execute against.

    ``connection`` is required by the schema and is also the RBAC fence: only the
    platform team holds one named ``platform``, so under REQUIRE_REFS a tenant
    team skips the whole dashboard rather than seeing operator SQL.
    """
    dashboard = json.loads(dashboard_files()[filename])

    for tile in _sql_tiles(dashboard):
        assert tile["config"].get("connection") == PLATFORM_CONNECTION, (
            f"{filename}:{tile['id']} does not bind to the platform connection"
        )


@pytest.mark.parametrize("filename", sorted(dashboard_files()))
def test_raw_sql_tiles_respect_the_time_range(filename):
    """A tile that ignores the picker reads as live data and is not."""
    dashboard = json.loads(dashboard_files()[filename])

    for tile in _sql_tiles(dashboard):
        if tile["id"] in POINT_IN_TIME_TILES | FIXED_WINDOW_TILES:
            continue
        sql = tile["config"]["sqlTemplate"]
        assert any(macro in sql for macro in TIME_MACROS), (
            f"{filename}:{tile['id']} has no time-range macro"
        )


def _overview_tiles() -> dict[str, dict]:
    return {tile["id"]: tile for tile in _dashboards()["dfe-overview.json"]["tiles"]}


def test_the_overview_is_named_overview():
    """The provisioner upserts on the name, so a rename seeds a second dashboard."""
    assert _dashboards()["dfe-overview.json"]["name"] == "Overview"


def test_each_fixed_window_tile_reads_the_seven_days_to_today():
    tiles = _overview_tiles()

    assert FIXED_WINDOW_TILES <= set(tiles)
    for tile_id in FIXED_WINDOW_TILES:
        sql = tiles[tile_id]["config"]["sqlTemplate"]
        assert SEVEN_DAYS_TO_TODAY in sql, tile_id
        # max() over no rows answers 0, which would read as a measured zero.
        assert "maxOrNull(value)" in sql, tile_id


def test_the_overview_titles_match_the_usage_api():
    """The console strip and the dashboard name each number the same way."""
    from dfe_engine.api.v1.usage import HIGHEST_LABEL, METRIC_LABELS

    dashboard = _dashboards()["dfe-overview.json"]
    containers = {container["id"]: container["title"] for container in dashboard["containers"]}
    tiles = _overview_tiles()
    assert containers["highest"] == HIGHEST_LABEL
    for metric, label in METRIC_LABELS.items():
        slug = metric.replace("_", "-")
        assert tiles[f"highest-{slug}"]["config"]["name"] == label
        assert tiles[f"per-day-{slug}"]["config"]["name"] == label
        assert f"metric = '{metric}'" in tiles[f"per-day-{slug}"]["config"]["sqlTemplate"]


def test_committed_offset_tiles_skip_the_revoke_marker():
    """No tile may read a revoke's 0 as the partition's position.

    ``max(Value) - min(Value)`` over a window holding that 0 reports the partition's
    whole committed offset as progress.
    """
    tiles = [
        (filename, tile)
        for filename, dashboard in _dashboards().items()
        for tile in _sql_tiles(dashboard)
        if f"'{COMMITTED_OFFSET_METRIC}'" in tile["config"]["sqlTemplate"]
    ]

    assert tiles, f"no tile reads {COMMITTED_OFFSET_METRIC}, so this guard checks nothing"
    for filename, tile in tiles:
        assert "Value > 0" in tile["config"]["sqlTemplate"], (
            f"{filename}:{tile['id']} counts a revoked partition's 0 as an offset"
        )


def test_counter_tiles_difference_each_series_on_its_own():
    """No tile subtracts one pod's counter from another's.

    ``max(Value) - min(Value)`` over a sum metric spans every replica's series, so two
    loaders at 680 and 5 read 675. tests/integration/test_services_dashboard_ch.py runs
    the tiles against ClickHouse; this holds every tile to the shape without one.
    """
    tiles = [
        (filename, tile)
        for filename, dashboard in _dashboards().items()
        for tile in _sql_tiles(dashboard)
        if "otel_metrics_sum" in tile["config"]["sqlTemplate"]
    ]

    for filename, tile in tiles:
        sql = tile["config"]["sqlTemplate"]
        assert "max(Value) - min(Value)" not in sql, (
            f"{filename}:{tile['id']} subtracts across every series of a counter"
        )
        if "arrayDifference" in sql:
            assert COUNTER_SERIES_KEY in sql, (
                f"{filename}:{tile['id']} differences a counter without the per-series key"
            )
    differenced = [tile for _, tile in tiles if "arrayDifference" in tile["config"]["sqlTemplate"]]
    assert differenced, "no tile differences a counter, so this guard checks nothing"


@pytest.mark.parametrize("filename", sorted(dashboard_files()))
def test_tiles_reference_declared_containers(filename):
    """A tile pointing at a container that does not exist renders unplaced."""
    dashboard = json.loads(dashboard_files()[filename])
    declared = {c["id"] for c in dashboard.get("containers", [])}

    for tile in dashboard["tiles"]:
        container = tile.get("containerId")
        if container is not None:
            assert container in declared, (
                f"{filename}:{tile['id']} names undeclared container {container}"
            )


@pytest.mark.parametrize(
    "filename",
    [
        "dfe-pipeline-health.json",
        "dfe-clickhouse-health.json",
        "dfe-clickhouse-overview.json",
        "dfe-clickhouse-internals.json",
        "dfe-services.json",
        "dfe-kafka.json",
        "dfe-overview.json",
    ],
)
def test_platform_dashboards_are_fenced_by_their_sources(filename):
    """Operator telemetry must not reach a tenant.

    The fence is the source set, not a flag: each platform dashboard has to name
    at least one platform-only source, or it resolves on a tenant team and seeds
    there. Also asserted by the tag, which is what an operator reads.
    """
    dashboard = json.loads(dashboard_files()[filename])

    assert _tile_sources(dashboard) & PLATFORM_SOURCES
    assert "platform" in dashboard["tags"]


def test_export_writes_every_dashboard(tmp_path):
    """The init container's whole job, end to end."""
    written = export_dashboards(tmp_path)

    assert {p.name for p in written} == set(dashboard_files())
    for path in written:
        assert json.loads(path.read_text(encoding="utf-8"))


def test_export_creates_a_missing_directory(tmp_path):
    """The mount point is an empty volume, so the directory may not exist yet."""
    target = tmp_path / "nested" / "dashboards"

    export_dashboards(target)

    assert target.is_dir()


def test_export_overwrites_a_stale_file_but_keeps_site_local_ones(tmp_path):
    """An older image's copy is replaced; an operator's own file is left alone."""
    stale = tmp_path / "dfe-throughput.json"
    stale.write_text('{"name": "old"}', encoding="utf-8")
    site_local = tmp_path / "site-local.json"
    site_local.write_text('{"name": "mine"}', encoding="utf-8")

    export_dashboards(tmp_path)

    assert json.loads(stale.read_text(encoding="utf-8"))["name"] == "DFE Throughput"
    assert site_local.read_text(encoding="utf-8") == '{"name": "mine"}'


def test_export_is_idempotent(tmp_path):
    """The init container reruns on every pod restart."""
    first = export_dashboards(tmp_path)
    contents = {p.name: p.read_text(encoding="utf-8") for p in first}

    export_dashboards(tmp_path)

    assert {p.name: p.read_text(encoding="utf-8") for p in first} == contents
