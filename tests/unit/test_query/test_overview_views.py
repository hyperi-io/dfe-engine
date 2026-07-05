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
_EXPECTED = {
    "overview/ingest_rows_bytes",
    "overview/ingest_by_source",
    "overview/pipeline_lag",
    "overview/hunt_executions",
    "overview/hunt_cost_leaderboard",
    "overview/detections",
    "overview/alerts",
    "overview/storage_growth",
    "overview/active_sources",
    "overview/hunt_fleet_health",
}


def test_ten_overview_views_present():
    assert len(_OVERVIEW) == 10
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
        "dfe_v_overview_ingest_rows_bytes",
        "dfe_v_overview_ingest_by_source",
        "dfe_v_overview_pipeline_lag",
        "dfe_v_overview_hunt_executions",
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
