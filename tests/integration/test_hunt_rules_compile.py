#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_rules_compile.py
#  Purpose:      A hunt created through the API detects, on real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule and a hunt made the way the API makes them land rows in dfe.detection.

This is #272's done-when stated as something you can run. Both files on disk come
from the models the API writes them with -- ``Rule`` through the ``RuleRegistry``,
the hunt through ``HuntCreateRequest.to_config_dict`` -- so nothing here is shaped
to suit the runner. Nobody hand-edits the YAML: the loader compiles the hunt's rule
names into its SQL, the worker substitutes the window, and the detection row is the
proof it ran.

The tables are the REAL default and detection built from the core specs, so the
window arithmetic meets the common header's DateTime64(3) ``_timestamp_load``
rather than an integer column chosen to make the test pass.

Deterministic: the fire time is computed from the hunt's own phase offset, and one
of the two source rows sits outside the window, so the single detection row pins
the window as well as the compile.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from dfe_engine.api.v1.hunts import HuntCreateRequest
from dfe_engine.hunt_runner import ChCoordinator, HuntRunner, HuntWorker, load_specs
from dfe_engine.hunt_runner.spread import current_fire
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.yaml_utils import yaml_dump_string

_INTERVAL = 60  # "* * * * *", the cron the API's own create form offers


def _write_rule(rules_dir: Path, rule_id: str, marker: str) -> None:
    """Write the rule file exactly as POST /api/v1/rules writes it."""
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(
            Rule(
                rule_id=rule_id,
                name="Marked Org Activity",
                severity="high",
                where_clause=f"_org_id = '{marker}'",
            )
        )
    finally:
        registry.close()


def _write_hunt(hunts_dir: Path, hunt: str, rule_id: str, db: str) -> None:
    """Write the hunt file exactly as POST /api/v1/hunts writes it."""
    body = HuntCreateRequest(
        name=hunt,
        cron="* * * * *",
        rules=[rule_id],
        customers=["acme"],
        global_source_table_name=f"{db}.main",
        global_target_table_name=f"{db}.detection",
    )
    hunts_dir.mkdir(parents=True, exist_ok=True)
    (hunts_dir / f"{hunt}.yaml").write_text(
        yaml_dump_string(body.to_config_dict(hunt_name=hunt)),
        encoding="utf-8",
        newline="\n",
    )


def test_a_hunt_created_through_the_api_writes_detections(ch_client, dfe_db, tmp_path):
    hunt = f"api_hunt_{uuid.uuid4().hex[:8]}"
    rule_id = f"{hunt}_rule"
    marker = f"org-{uuid.uuid4().hex[:8]}"

    _write_rule(tmp_path / "rules", rule_id, marker)
    _write_hunt(tmp_path / "hunts", hunt, rule_id, dfe_db)

    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    ch_client.command(
        f"INSERT INTO `{dfe_db}`.`default` (_timestamp_load, _timestamp, _org_id) VALUES "
        f"(toDateTime64({fire - 1}, 3), toDateTime64({fire - 1}, 3), '{marker}'), "
        f"(toDateTime64({fire - 2 * _INTERVAL}, 3), toDateTime64({fire}, 3), '{marker}')"
    )

    specs = load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules")
    assert set(specs) == {hunt}
    assert specs[hunt].queries, "the hunt's rules compiled to nothing to run"

    coord = ChCoordinator(ch_client, database=dfe_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    runner = HuntRunner(coord, HuntWorker(ch_client, coord), specs, cap=8)
    assert runner.tick(fire + 1) == 1
    assert coord.get_watermark(hunt) == fire

    rows = ch_client.query(
        "SELECT hunt_name, rule_id, rule_name, source_table, severity, "
        f"toUnixTimestamp(_timestamp), _org_id FROM `{dfe_db}`.detection "
        "WHERE hunt_name = {h:String}",
        parameters={"h": hunt},
    ).result_rows
    # One row: the [fire-60, fire) window took the recent row and left the older one,
    # which is in the table and outside the window.
    assert len(rows) == 1
    assert tuple(rows[0][:5]) == (hunt, rule_id, "Marked Org Activity", "default", "high")
    assert int(rows[0][5]) == fire - 1
    assert rows[0][6] == marker
