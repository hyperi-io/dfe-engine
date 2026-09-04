#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_spec_loader.py
#  Purpose:      Load hunt YAML defs -> HuntSpec (rate-only, skip anchored/bad)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""spec_loader turns gitops hunt YAML files into HuntSpec objects the runner ticks.

Real YAML files on disk (tmp_path), no mocks - the loader's job is exactly the
file->model translation, so the fixtures ARE the contract: the filename stem is the
hunt_id (never an in-file id), rate schedules resolve to interval_seconds, anchored
and malformed hunts are skipped per-file without sinking the rest of the load.
"""

from __future__ import annotations

from pathlib import Path

from dfe_engine.hunt_runner.spec_loader import load_specs


def _write(hunt_dir: Path, name: str, body: str) -> None:
    """Write a hunt YAML file <name>.yaml into hunt_dir (created if needed)."""
    hunt_dir.mkdir(parents=True, exist_ok=True)
    (hunt_dir / f"{name}.yaml").write_text(body)


def test_rate_schedule_interval_duration(tmp_path: Path):
    _write(
        tmp_path,
        "brute_force",
        'schedule:\n  mode: rate\n  interval: "5m"\n',
    )
    specs = load_specs(tmp_path)
    assert set(specs) == {"brute_force"}
    assert specs["brute_force"].interval_seconds == 300


def test_legacy_flat_cron_string(tmp_path: Path):
    _write(tmp_path, "port_scan", 'cron: "*/10 * * * *"\n')
    specs = load_specs(tmp_path)
    assert specs["port_scan"].interval_seconds == 600


def test_legacy_cron_list_takes_min(tmp_path: Path):
    # Two schedules -> the runner must keep up with the TIGHTEST (2m == 120s).
    _write(
        tmp_path,
        "exfil",
        'cron:\n  - "*/10 * * * *"\n  - "*/2 * * * *"\n',
    )
    specs = load_specs(tmp_path)
    assert specs["exfil"].interval_seconds == 120


def test_anchored_schedule_is_skipped(tmp_path: Path):
    # anchored is recognised but not runnable in v1 -> absent from the result.
    _write(
        tmp_path,
        "weekly_report",
        'schedule:\n  mode: anchored\n  cron: "0 2 * * 0"\n',
    )
    specs = load_specs(tmp_path)
    assert "weekly_report" not in specs
    assert specs == {}


def test_malformed_hunt_skipped_but_sibling_loads(tmp_path: Path):
    # A bad interval must not sink the whole directory - skip is strictly per-file.
    _write(
        tmp_path,
        "broken",
        'schedule:\n  mode: rate\n  interval: "banana"\n',
    )
    _write(
        tmp_path,
        "good",
        'schedule:\n  mode: rate\n  interval: "5m"\n',
    )
    specs = load_specs(tmp_path)
    assert "broken" not in specs
    assert "good" in specs
    assert specs["good"].interval_seconds == 300


def test_zero_interval_is_skipped(tmp_path: Path):
    # A zero interval would divide-by-zero in the spread maths -> skip it.
    _write(
        tmp_path,
        "zero",
        'schedule:\n  mode: rate\n  interval: "0s"\n',
    )
    specs = load_specs(tmp_path)
    assert "zero" not in specs


def test_hunt_id_comes_from_filename_not_infile_field(tmp_path: Path):
    # The YAML carries a bogus id/name; identity MUST be the filename stem.
    _write(
        tmp_path,
        "real_identity",
        'id: not_this\nname: also_not_this\nschedule:\n  mode: rate\n  interval: "5m"\n',
    )
    specs = load_specs(tmp_path)
    assert "real_identity" in specs
    assert "not_this" not in specs
    assert "also_not_this" not in specs
    assert specs["real_identity"].hunt_id == "real_identity"


def test_query_target_and_timestamp_fields_read(tmp_path: Path):
    _write(
        tmp_path,
        "detail",
        'schedule:\n  mode: rate\n  interval: "1m"\n'
        'query: "SELECT * FROM src WHERE ts > {window}"\n'
        'global_target_table_name: "dfe_hunts.results"\n'
        'timestamp_field: "event_time"\n',
    )
    spec = load_specs(tmp_path)["detail"]
    assert spec.query == "SELECT * FROM src WHERE ts > {window}"
    assert spec.target_table == "dfe_hunts.results"
    assert spec.timestamp_field == "event_time"


def test_timestamp_field_defaults_to_timestamp_load(tmp_path: Path):
    _write(
        tmp_path,
        "defaults",
        'schedule:\n  mode: rate\n  interval: "1m"\n',
    )
    spec = load_specs(tmp_path)["defaults"]
    assert spec.timestamp_field == "timestamp_load"
    assert spec.query == ""
    assert spec.target_table == ""


def test_missing_directory_returns_empty_dict(tmp_path: Path):
    missing = tmp_path / "does_not_exist"
    assert load_specs(missing) == {}


def test_a_hunt_the_api_writes_loads_with_no_query_to_run(tmp_path: Path):
    """The API's own hunt YAML schedules and executes nothing. Pinned, not endorsed.

    ``POST /api/v1/hunts`` writes ``rules`` -- rule template names -- and the loader
    only ever reads ``query``. Nothing compiles the first into the second, so a hunt
    created in the UI claims its lease, advances its watermark and detects nothing.
    Built from the API's own request model so this turns red the day that is wired,
    which is the point of pinning it here.
    """
    from dfe_engine.api.v1.hunts import HuntCreateRequest
    from dfe_engine.yaml_utils import yaml_dump_string

    body = HuntCreateRequest(
        name="api_hunt",
        cron="* * * * *",
        rules=["some_rule"],
        customers=["acme"],
        global_source_table_name="dfe.default",
        global_target_table_name="dfe.detection",
        checkpoint_timestamp_field="_timestamp_load",
    )
    config = body.to_config_dict(hunt_name="api_hunt")
    _write(tmp_path, "api_hunt", yaml_dump_string(config))

    spec = load_specs(tmp_path)["api_hunt"]
    assert spec.interval_seconds == 60  # the schedule survives
    assert spec.query == ""  # and there is nothing to run
    # checkpoint_timestamp_field is the API's name for it; the loader reads
    # timestamp_field, so the operator's choice does not reach the runner either.
    assert "checkpoint_timestamp_field" in config
    assert spec.timestamp_field == "timestamp_load"
