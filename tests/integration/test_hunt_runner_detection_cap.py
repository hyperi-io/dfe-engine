#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_runner_detection_cap.py
#  Purpose:      A rule that matches too much is cut to the cap and says so, on real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule over its per-run cap writes the cap, then one row saying how many it matched.

The rule and the hunt are written the way the API writes them, the tables are the
REAL landing, detection and coordination tables, and the counters go through
scalo's own metrics manager. So what is asserted is what a deployment would see:
the detection rows, the one summary row with the true match count, the watermark,
the run record the Hunts page reads, and the two counters.

The flood rule reads a ``_json`` path as well as ``_org_id``, because the summary
count runs the rule's WHERE again and a summary expression named ``_json`` would
shadow the source column there.
"""

import json
import time
import uuid

from common.hunt_files import write_hunt, write_rule
from prometheus_client.parser import text_string_to_metric_families
from scalo.logger import logger
from scalo.metrics import create_metrics

from dfe_engine.hunt_runner import (
    ChCoordinator,
    HuntRunner,
    HuntRunnerMetrics,
    HuntWorker,
    load_specs,
    read_run_status,
)
from dfe_engine.hunt_runner.spread import current_fire
from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.yaml_utils import yaml_dump_string

_INTERVAL = 60  # "* * * * *", the cron the API's own create form offers
_SHIPPED_CAP = 1000  # the decided default: detections one rule may write in one run
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _land(ch_client, db: str, org: str, fire: int, count: int, *, seconds_before: int = 1) -> None:
    """Land *count* flood events for *org*, the newest *seconds_before* ahead of *fire*.

    They spread over 50 seconds, so the default lands them all in [fire - 60, fire).
    """
    newest = fire - seconds_before
    ch_client.command(
        f"INSERT INTO `{db}`.`main` (_timestamp_load, _timestamp, _org_id, _json) "
        f"SELECT toDateTime64({newest} - number % 50, 3), toDateTime64({newest} - number % 50, 3), "
        f"'{org}', '{{\"kind\":\"flood\"}}' FROM numbers({count})"
    )


def _metrics() -> tuple[object, HuntRunnerMetrics]:
    """scalo's own manager on its in-process backend, so the samples can be read back."""
    manager = create_metrics(
        "dfe-hunt-runner", backend="prometheus", enable_auto_update=False, metric_prefix="dfe"
    )
    return manager, HuntRunnerMetrics(manager)


def _sample(manager, name: str, **labels: str) -> float:
    """One sample off the exposition text, 0.0 when the series was never touched."""
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return 0.0


def _specs(tmp_path, **kwargs) -> dict:
    """The hunts under *tmp_path*, compiled off the rules beside them."""
    return load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules", **kwargs)


def _tick(ch_client, db: str, specs: dict, metrics: HuntRunnerMetrics, fire: int) -> ChCoordinator:
    coord = ChCoordinator(ch_client, database=db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    runner = HuntRunner(coord, HuntWorker(ch_client, coord, metrics=metrics), specs, cap=8)
    assert runner.tick(fire + 1) == 1
    return coord


def _detections(ch_client, db: str, hunt: str) -> tuple[int, int]:
    """(detection rows, summary rows) the hunt wrote. A summary carries the nil UUID."""
    row = ch_client.query(
        "SELECT count(), countIf(matched_uuid = toUUID({nil:String})) "
        f"FROM `{db}`.detection WHERE hunt_name = {{h:String}}",
        parameters={"h": hunt, "nil": _NIL_UUID},
    ).result_rows[0]
    total, summaries = int(row[0]), int(row[1])
    return total - summaries, summaries


def _summary(ch_client, db: str, hunt: str) -> tuple[str, str, dict]:
    """(rule_id, _org_id, the parsed _json) of the hunt's one summary row."""
    rows = ch_client.query(
        "SELECT rule_id, _org_id, toJSONString(_json) "
        f"FROM `{db}`.detection WHERE hunt_name = {{h:String}} "
        "AND matched_uuid = toUUID({nil:String})",
        parameters={"h": hunt, "nil": _NIL_UUID},
    ).result_rows
    assert len(rows) == 1
    return str(rows[0][0]), str(rows[0][1]), json.loads(rows[0][2])


def _run_status(ch_client, db: str, hunt: str, fire: int) -> str:
    """The run record's current status for this fire, as the coordination table has it."""
    return str(
        ch_client.query(
            f"SELECT argMax(status, updated) FROM `{db}`.hunt_run "
            "WHERE hunt_id = {h:String} AND fire = {f:Int64}",
            parameters={"h": hunt, "f": fire},
        ).result_rows[0][0]
    )


def _rule_hunt(tmp_path, db: str, where_clause: str) -> tuple[str, str]:
    hunt = f"cap_{uuid.uuid4().hex[:8]}"
    rule_id = f"{hunt}_rule"
    write_rule(tmp_path / "rules", rule_id, where_clause)
    write_hunt(tmp_path / "hunts", hunt, rule_id, db)
    return hunt, rule_id


def test_a_rule_over_the_cap_writes_the_cap_and_one_summary_row(ch_client, dfe_db, tmp_path):
    org = f"org-{uuid.uuid4().hex[:8]}"
    hunt, rule_id = _rule_hunt(tmp_path, dfe_db, f"_org_id = '{org}' AND _json.kind = 'flood'")
    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    matched = _SHIPPED_CAP + 250
    _land(ch_client, dfe_db, org, fire, matched)

    captured: list = []
    handler_id = logger.add(captured.append, level="WARNING")
    try:
        manager, metrics = _metrics()
        coord = _tick(ch_client, dfe_db, _specs(tmp_path), metrics, fire)
    finally:
        logger.remove(handler_id)

    assert _detections(ch_client, dfe_db, hunt) == (_SHIPPED_CAP, 1)
    warnings = [m.record["extra"] for m in captured if "hit its detection cap" in m]
    assert [(w["rule_id"], w["matched"], w["written"], w["dropped"]) for w in warnings] == [
        (rule_id, matched, _SHIPPED_CAP, 250)
    ]
    assert _summary(ch_client, dfe_db, hunt) == (
        rule_id,
        org,
        {"dfe_capped": True, "matched": matched, "written": _SHIPPED_CAP, "rule_id": rule_id},
    )
    # The window is consumed: holding it would re-run the same flood every fire.
    assert coord.get_watermark(hunt) == fire
    assert _run_status(ch_client, dfe_db, hunt, fire) == "completed"
    status = read_run_status(ch_client, dfe_db, [hunt], now=fire + 1)[hunt]
    assert status.last_run == fire
    assert status.last_run_rows == _SHIPPED_CAP + 1
    labels = {"hunt_id": hunt, "rule_id": rule_id}
    assert _sample(manager, "dfe_hunt_detections_capped_total", **labels) == 1.0
    assert _sample(manager, "dfe_hunt_detections_dropped_total", **labels) == 250.0


def test_a_rule_under_the_cap_writes_every_match_and_no_summary(ch_client, dfe_db, tmp_path):
    org = f"org-{uuid.uuid4().hex[:8]}"
    hunt, rule_id = _rule_hunt(tmp_path, dfe_db, f"_org_id = '{org}' AND _json.kind = 'flood'")
    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    _land(ch_client, dfe_db, org, fire, 3)
    # Outside the window, and inside it for another org: neither may be written.
    _land(ch_client, dfe_db, org, fire, 1, seconds_before=2 * _INTERVAL)
    _land(ch_client, dfe_db, f"{org}-other", fire, 1)

    manager, metrics = _metrics()
    coord = _tick(ch_client, dfe_db, _specs(tmp_path), metrics, fire)

    assert _detections(ch_client, dfe_db, hunt) == (3, 0)
    assert coord.get_watermark(hunt) == fire
    assert read_run_status(ch_client, dfe_db, [hunt], now=fire + 1)[hunt].last_run_rows == 3
    labels = {"hunt_id": hunt, "rule_id": rule_id}
    assert _sample(manager, "dfe_hunt_detections_capped_total", **labels) == 0.0


def test_a_rule_that_matches_exactly_the_cap_drops_nothing_and_writes_no_summary(
    ch_client, dfe_db, tmp_path
):
    org = f"org-{uuid.uuid4().hex[:8]}"
    hunt, rule_id = _rule_hunt(tmp_path, dfe_db, f"_org_id = '{org}'")
    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    _land(ch_client, dfe_db, org, fire, _SHIPPED_CAP)

    manager, metrics = _metrics()
    _tick(ch_client, dfe_db, _specs(tmp_path), metrics, fire)

    assert _detections(ch_client, dfe_db, hunt) == (_SHIPPED_CAP, 0)
    labels = {"hunt_id": hunt, "rule_id": rule_id}
    assert _sample(manager, "dfe_hunt_detections_capped_total", **labels) == 0.0


def test_a_flood_across_orgs_names_no_single_org_on_its_summary(ch_client, dfe_db, tmp_path):
    """A summary counts every tenant's matches, so it must not sit under one of them."""
    marker = uuid.uuid4().hex[:8]
    orgs = (f"org-a-{marker}", f"org-b-{marker}")
    hunt, rule_id = _rule_hunt(tmp_path, dfe_db, f"_org_id IN ('{orgs[0]}', '{orgs[1]}')")
    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    for org in orgs:
        _land(ch_client, dfe_db, org, fire, 600)

    _manager, metrics = _metrics()
    _tick(ch_client, dfe_db, _specs(tmp_path), metrics, fire)

    assert _detections(ch_client, dfe_db, hunt) == (_SHIPPED_CAP, 1)
    summary_rule, summary_org, body = _summary(ch_client, dfe_db, hunt)
    assert (summary_rule, summary_org) == (rule_id, "")
    assert body["matched"] == 1200


def test_a_count_the_server_refuses_leaves_the_capped_rows_and_advances(
    ch_client, dfe_db, tmp_path
):
    """The capped INSERT is committed, so a failed count must not hold the window."""
    org = f"org-{uuid.uuid4().hex[:8]}"
    hunt, rule_id = _rule_hunt(tmp_path, dfe_db, f"_org_id = '{org}'")
    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    _land(ch_client, dfe_db, org, fire, 40)
    spec = _specs(tmp_path, max_detections=25)[hunt]
    statement = spec.queries[0]
    refused = statement.model_copy(update={"count_sql": "SELECT throwIf(1, 'count refused')"})
    specs = {hunt: spec.model_copy(update={"queries": [refused]})}

    captured: list = []
    handler_id = logger.add(captured.append, level="ERROR")
    try:
        manager, metrics = _metrics()
        coord = _tick(ch_client, dfe_db, specs, metrics, fire)
    finally:
        logger.remove(handler_id)

    assert _detections(ch_client, dfe_db, hunt) == (25, 0)
    assert coord.get_watermark(hunt) == fire
    assert _run_status(ch_client, dfe_db, hunt, fire) == "completed"
    failures = [m for m in captured if "cap summary failed" in m]
    assert len(failures) == 1
    assert failures[0].record["extra"]["rule_id"] == rule_id
    labels = {"hunt_id": hunt, "rule_id": rule_id}
    assert _sample(manager, "dfe_hunt_detections_capped_total", **labels) == 0.0


def test_a_direct_query_hunt_is_run_as_written_and_not_capped(ch_client, dfe_db, tmp_path):
    """A pre-compiled ``query`` carries its own SQL, so no LIMIT can be put into it."""
    org = f"org-{uuid.uuid4().hex[:8]}"
    hunt = f"direct_{uuid.uuid4().hex[:8]}"
    query = HuntResultSchema().build_insert_select(
        target_db=dfe_db,
        target_table="detection",
        source_db=dfe_db,
        source_table="main",
        where_clause=f"_org_id = '{org}'",
        rule_id=hunt,
        rule_name=hunt,
        hunt_name=hunt,
        severity="high",
        timestamp_placeholder="{window}",
    )
    hunts_dir = tmp_path / "hunts"
    hunts_dir.mkdir()
    (hunts_dir / f"{hunt}.yaml").write_text(
        yaml_dump_string({"schedule": {"mode": "rate", "interval": "1m"}, "query": query}),
        encoding="utf-8",
        newline="\n",
    )
    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    matched = _SHIPPED_CAP + 250
    _land(ch_client, dfe_db, org, fire, matched)

    _manager, metrics = _metrics()
    _tick(ch_client, dfe_db, load_specs(hunts_dir), metrics, fire)

    assert _detections(ch_client, dfe_db, hunt) == (matched, 0)
