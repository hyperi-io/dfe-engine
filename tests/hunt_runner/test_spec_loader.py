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

from pathlib import Path

from common.hunt_files import write_hunt, write_rule
from scalo.logger import logger

from dfe_engine.hunt_runner.models import HuntStatement
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
    assert spec.queries == [HuntStatement(sql="SELECT * FROM src WHERE ts > {window}")]
    assert spec.target_table == "dfe_hunts.results"
    assert spec.timestamp_field == "event_time"


def test_timestamp_field_defaults_to_the_common_header_load_column(tmp_path: Path):
    _write(
        tmp_path,
        "defaults",
        'schedule:\n  mode: rate\n  interval: "1m"\n',
    )
    spec = load_specs(tmp_path)["defaults"]
    assert spec.timestamp_field == "_timestamp_load"
    assert spec.queries == []
    assert spec.target_table == ""


def test_checkpoint_timestamp_field_is_read_as_the_watermark_column(tmp_path: Path):
    # checkpoint_timestamp_field is the API's name for it, and it used to be ignored.
    _write(
        tmp_path,
        "api_named",
        'schedule:\n  mode: rate\n  interval: "1m"\ncheckpoint_timestamp_field: "_timestamp"\n',
    )
    assert load_specs(tmp_path)["api_named"].timestamp_field == "_timestamp"


def test_missing_directory_returns_empty_dict(tmp_path: Path):
    missing = tmp_path / "does_not_exist"
    assert load_specs(missing) == {}


def test_a_hunt_the_api_writes_loads_with_a_query_to_run(tmp_path: Path):
    """The API's own hunt YAML loads into a spec that has SQL to execute.

    ``POST /api/v1/hunts`` writes ``rules`` -- rule names -- and ``POST
    /api/v1/rules`` writes the rule file each names. Both are built from the API's
    own models here, so this fails the day the compile path stops reaching the
    runner rather than the day someone notices detections stopped.
    """
    from dfe_engine.api.v1.hunts import HuntCreateRequest
    from dfe_engine.hunts.rule_model import Rule
    from dfe_engine.hunts.rule_registry import RuleRegistry
    from dfe_engine.yaml_utils import yaml_dump_string

    rules_dir = tmp_path / "rules"
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(
            Rule(
                rule_id="some_rule",
                name="Certutil Abuse",
                severity="high",
                where_clause="process_name = 'certutil.exe'",
            )
        )
    finally:
        registry.close()

    body = HuntCreateRequest(
        name="api_hunt",
        cron="* * * * *",
        rules=["some_rule"],
        customers=["acme"],
        global_source_table_name="dfe.main",
        global_target_table_name="dfe.detection",
        checkpoint_timestamp_field="_timestamp_load",
    )
    config = body.to_config_dict(hunt_name="api_hunt")
    hunts_dir = tmp_path / "hunts"
    _write(hunts_dir, "api_hunt", yaml_dump_string(config))

    spec = load_specs(hunts_dir, rules_dir=rules_dir)["api_hunt"]
    assert spec.interval_seconds == 60  # the schedule survives
    assert len(spec.queries) == 1
    sql = spec.queries[0].sql
    assert sql.startswith("INSERT INTO dfe.detection")
    assert "FROM dfe.main" in sql
    assert "process_name = 'certutil.exe'" in sql
    assert "{window}" in sql  # the worker substitutes the incremental predicate
    assert "'some_rule' AS rule_id" in sql
    assert "'api_hunt' AS hunt_name" in sql
    assert "'high' AS severity" in sql
    # checkpoint_timestamp_field is the API's name for the watermark column, and it
    # now reaches the runner rather than being dropped for the loader's default.
    assert spec.timestamp_field == "_timestamp_load"


def test_a_compiled_rule_carries_the_cap_the_loader_was_given(tmp_path: Path):
    write_rule(tmp_path / "rules", "noisy_rule", "a = 1")
    write_hunt(tmp_path / "hunts", "noisy", "noisy_rule", "dfe")

    spec = load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules", max_detections=40)["noisy"]

    assert [(s.rule_id, s.cap) for s in spec.queries] == [("noisy_rule", 40)]
    assert spec.queries[0].sql.endswith("\nLIMIT 40")


def test_a_direct_query_is_run_uncapped_and_the_load_says_so(tmp_path: Path):
    _write(
        tmp_path,
        "hand_written",
        'schedule:\n  mode: rate\n  interval: "1m"\n'
        'query: "INSERT INTO dfe.detection SELECT * FROM dfe.main WHERE {window}"\n',
    )
    captured: list = []
    handler_id = logger.add(captured.append, level="INFO")
    try:
        spec = load_specs(tmp_path, max_detections=40)["hand_written"]
    finally:
        logger.remove(handler_id)

    assert [(s.cap, s.count_sql, s.summary_sql) for s in spec.queries] == [(0, "", "")]
    assert "LIMIT" not in spec.queries[0].sql
    lines = [m for m in captured if "detection cap does not apply" in m]
    assert len(lines) == 1
    assert lines[0].record["extra"]["hunt_id"] == "hand_written"
