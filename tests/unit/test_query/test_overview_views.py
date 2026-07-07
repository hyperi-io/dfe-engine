"""The builtin ``overview/`` hero-dashboard views.

These are system-scope throughput/health views over the REAL landing / audit /
hunt-coordination tables (dfe.default, dfe_hunts.detection, dfe_audit.*, the
hunt_* coordination tables, system.parts). System-scope means NO ``org_id``
parameter, so the catalog marks them non-tenant-isolated (the executor gates
those to the admin surface). This test pins their shape without a live
ClickHouse: names follow the convention, params parse, and none silently became
tenant views.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from dfe_engine.query.catalog import parse_view_parameters, view_name_to_label
from dfe_engine.query.ddl import BUILTIN_VIEWS_DIR, DDLManager

_OVERVIEW = sorted(BUILTIN_VIEWS_DIR.glob("dfe_v_overview_*.sql"))
# The overall-throughput view (ingest_rows_bytes) is ENGINE-GENERATED (it must
# merge() over exactly the dynamic set of _timestamp_load data tables), so it is NOT
# a static .sql - see DDLManager.apply_throughput_view / the tests at the bottom.
# alerts + hunt_executions + hunt_cost_leaderboard remain PARKED (no writer table);
# hunt_cost_leaderboard is now un-parkable via dfe_audit.query_log_archive.
_EXPECTED = {
    "overview/ingest_by_source",
    "overview/pipeline_lag",
    "overview/detections",
    "overview/storage_growth",
    "overview/active_sources",
    "overview/hunt_fleet_health",
}


def test_six_static_overview_views_present():
    assert len(_OVERVIEW) == 6
    labels = {view_name_to_label(f.stem) for f in _OVERVIEW}
    assert labels == _EXPECTED


def test_each_overview_view_is_well_formed():
    for f in _OVERVIEW:
        stem = f.stem
        text = f.read_text()
        # The DDL applier keys off the file stem being the view name.
        assert text.lstrip().startswith(f"CREATE OR REPLACE VIEW {stem} AS"), stem
        # Params must parse (no malformed {name:Type} placeholders).
        params = parse_view_parameters(text)
        names = {p.name for p in params}
        # System-scope: never an org_id param (that would flip it tenant-isolated).
        assert "org_id" not in names, f"{stem} unexpectedly tenant-scoped"


def test_time_windowed_views_expose_bucket_and_range():
    # The interval-series views take a caller-supplied window + bucket size.
    windowed = {
        "dfe_v_overview_ingest_by_source",
        "dfe_v_overview_pipeline_lag",
        "dfe_v_overview_detections",
    }
    by_stem = {f.stem: f for f in _OVERVIEW}
    for stem in windowed:
        names = {p.name for p in parse_view_parameters(by_stem[stem].read_text())}
        assert {"time_from", "time_to", "bucket_minutes"} <= names, stem


def test_overview_views_are_applied_by_ddl_manager():
    # Glob-discovered: no code registration needed, so a fresh apply picks them up.
    mgr = DDLManager(client=MagicMock(), database="default")
    applied = mgr.apply_all_builtin_views()
    for label in _EXPECTED:
        name = "dfe_v_" + label.replace("/", "_")
        assert name in applied


# ---------------------------------------------------------------------------
# The ENGINE-GENERATED overall-throughput view (rows/bytes per bucket across every
# _timestamp_load-bearing data table). Generated because merge('.*') errors on the
# coordination tables in the data db and source-table names are dynamic.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


def test_render_throughput_sql_merges_only_sensed_tables():
    sql = DDLManager._render_throughput_sql(["default", "apache", "win_events"])
    assert "FROM merge({db}, '^(default|apache|win_events)$')" in sql
    assert sql.lstrip().startswith("CREATE OR REPLACE VIEW dfe_v_overview_ingest_rows_bytes AS")
    # The histogram params (time filter + bucket) are preserved.
    params = {p.name for p in parse_view_parameters(sql)}
    assert {"time_from", "time_to", "bucket_minutes"} <= params


def test_render_throughput_escapes_regex_metachars():
    # A source label with a regex metachar must not widen the merge match.
    sql = DDLManager._render_throughput_sql(["a.b", "c+d"])
    assert r"^(a\.b|c\+d)$" in sql


def test_render_throughput_escapes_sql_single_quote():
    # re.escape leaves the SQL ' delimiter untouched; a quote-bearing table name
    # from out-of-band DDL must be doubled ('') so it cannot break out of the
    # merge() pattern literal.
    sql = DDLManager._render_throughput_sql(["ev'il"])
    assert "ev''il" in sql
    # The pattern literal is still balanced - no stray single quote escapes it.
    assert "'^(ev''il)$'" in sql


def test_apply_throughput_view_senses_and_applies():
    client = MagicMock()
    client.query.return_value = _FakeResult([("default",), ("apache",)])
    mgr = DDLManager(client=client, database="dfe")
    assert mgr.apply_throughput_view() is True
    # {db} resolved to the data database, merged exactly the two sensed tables.
    created_sql = client.command.call_args_list[0].args[0]
    assert "merge(dfe, '^(default|apache)$')" in created_sql


def test_apply_throughput_view_skips_when_no_data_tables():
    client = MagicMock()
    client.query.return_value = _FakeResult([])
    mgr = DDLManager(client=client, database="dfe")
    assert mgr.apply_throughput_view() is False
    client.command.assert_not_called()
