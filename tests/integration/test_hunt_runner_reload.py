#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_runner_reload.py
#  Purpose:      A hunt added while the loop runs is picked up without a restart
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt that arrives AFTER the runner started still runs, on real ClickHouse.

The runner's whole operating model rests on this: an operator creates a hunt in
the UI, the engine writes the YAML to the shared config dir, and the runner that
is already running picks it up on its next reload. Nothing restarts. The reload
CADENCE is covered by the pure daemon tests; what was not covered is the chain
those tests stub out - reload -> read the directory -> rebuild the runner ->
execute against ClickHouse -> a row in the real detection table.

So the loop starts against an EMPTY hunt dir, the hunt file is written from
inside the first tick, and the second tick has to find it. One ``run_loop`` call
covers both, which is what makes "without a restart" a fact about this test
rather than a claim in its name.

Deterministic by construction: the clock is a fixed even epoch second and the
interval is 2s, so the hunt's phase offset is 0 and its fire time IS that
instant. No sleeping, no waiting on a real schedule.

Neither hunt names a timestamp field, so the window rides the loader's default -
the common header's ``_timestamp_load`` on the real table.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from dfe_engine.hunt_runner import (
    ChCoordinator,
    HuntRunner,
    HuntWorker,
    load_specs,
    run_loop,
)
from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.yaml_utils import yaml_dump_string


def _hunt_query(*, db: str, hunt: str, marker: str, rule: str) -> str:
    """The hunt's INSERT ... SELECT, built by the product's own output composer."""
    return HuntResultSchema().build_insert_select(
        target_db=db,
        target_table="detection",
        source_db=db,
        source_table="default",
        where_clause=f"_org_id = '{marker}'",
        rule_id=rule,
        rule_name=rule,
        hunt_name=hunt,
        severity="high",
        timestamp_placeholder="{window}",
    )


def test_hunt_added_after_the_loop_started_runs_without_a_restart(ch_client, dfe_db, tmp_path):
    hunts_dir = tmp_path / "hunts"
    hunts_dir.mkdir()
    hunt = f"reload_{uuid.uuid4().hex[:8]}"
    rule = f"{hunt}_rule"
    marker = f"org-{uuid.uuid4().hex[:8]}"

    # An even second, so the 2s interval's boundary IS this instant and the hunt's
    # phase offset (a hash into a 1-second window) can only be 0.
    fixed_now = int(time.time()) // 2 * 2
    ch_client.command(
        f"INSERT INTO `{dfe_db}`.`default` (_timestamp_load, _timestamp, _org_id) VALUES "
        f"(toDateTime64({fixed_now - 1}, 3), toDateTime64({fixed_now - 1}, 3), '{marker}'), "
        f"(toDateTime64({fixed_now - 30}, 3), toDateTime64({fixed_now - 30}, 3), '{marker}')"
    )

    coord = ChCoordinator(ch_client, database=dfe_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)

    def _build_runner() -> HuntRunner:
        return HuntRunner(coord, worker, load_specs(hunts_dir), cap=8)

    # Nothing to run when the loop starts, which is the premise of the whole test.
    assert load_specs(hunts_dir) == {}
    cell = [_build_runner()]

    def _write_hunt() -> None:
        Path(hunts_dir / f"{hunt}.yaml").write_text(
            yaml_dump_string(
                {
                    "schedule": {"mode": "rate", "interval": "2s"},
                    "query": _hunt_query(db=dfe_db, hunt=hunt, marker=marker, rule=rule),
                    "global_target_table_name": f"{dfe_db}.detection",
                }
            ),
            encoding="utf-8",
            newline="\n",
        )

    executed: list[int] = []
    runner_ids: list[int] = []

    def _tick(now: int) -> int:
        runner_ids.append(id(cell[0]))
        ran = cell[0].tick(now)
        executed.append(ran)
        if len(executed) == 1:
            _write_hunt()  # the hunt arrives while the loop is running
        return ran

    run_loop(
        tick=_tick,
        should_stop=lambda: len(executed) >= 2,
        clock=lambda: float(fixed_now),
        sleep=lambda _s: None,
        poll_seconds=0.0,
        on_reload=lambda: cell.__setitem__(0, _build_runner()),
        reload_every=1,
    )

    # Tick 1 had nothing to run; tick 2 ran the hunt that appeared between them.
    assert executed == [0, 1]
    # A different runner object served tick 2, so the reload happened INSIDE the
    # loop rather than the loop being re-entered around it.
    assert runner_ids[0] != runner_ids[1]

    # The watermark advances only after the INSERT commits, so this is the run.
    assert coord.get_watermark(hunt) == fixed_now

    rows = ch_client.query(
        f"SELECT hunt_name, rule_name, rule_id, source_table, severity, "
        f"toUnixTimestamp(_timestamp) FROM `{dfe_db}`.detection "
        "WHERE hunt_name = {h:String}",
        parameters={"h": hunt},
    ).result_rows
    # Exactly one: the row inside the [fire-2, fire) window. The 30s-old row is in
    # the table and out of the window, so this also pins the window arithmetic.
    assert len(rows) == 1
    assert tuple(rows[0][:5]) == (hunt, rule, rule, "default", "high")
    assert int(rows[0][5]) == fixed_now - 1


def test_a_hunt_removed_while_the_loop_runs_stops_running(ch_client, dfe_db, tmp_path):
    """The other half of the reload: deleting the YAML stops the hunt, live.

    A hunt an operator deleted that keeps firing is worse than one that never
    started, because nothing in the UI says it is still there.
    """
    hunts_dir = tmp_path / "hunts"
    hunts_dir.mkdir()
    hunt = f"reload_{uuid.uuid4().hex[:8]}"
    marker = f"org-{uuid.uuid4().hex[:8]}"
    fixed_now = int(time.time()) // 2 * 2

    coord = ChCoordinator(ch_client, database=dfe_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)
    path = hunts_dir / f"{hunt}.yaml"
    path.write_text(
        yaml_dump_string(
            {
                "schedule": {"mode": "rate", "interval": "2s"},
                "query": _hunt_query(db=dfe_db, hunt=hunt, marker=marker, rule=hunt),
            }
        ),
        encoding="utf-8",
        newline="\n",
    )

    def _build_runner() -> HuntRunner:
        return HuntRunner(coord, worker, load_specs(hunts_dir), cap=8)

    cell = [_build_runner()]
    assert set(load_specs(hunts_dir)) == {hunt}
    ticks: list[int] = []

    def _tick(now: int) -> int:
        ran = cell[0].tick(now)
        ticks.append(ran)
        if len(ticks) == 1:
            path.unlink()
        return ran

    # Tick 2 sits on the NEXT interval, so a hunt still loaded would be due again.
    clock = {"now": float(fixed_now)}

    def _advance(_seconds: float) -> None:
        clock["now"] += 2

    run_loop(
        tick=_tick,
        should_stop=lambda: len(ticks) >= 2,
        clock=lambda: clock["now"],
        sleep=_advance,
        poll_seconds=0.0,
        on_reload=lambda: cell.__setitem__(0, _build_runner()),
        reload_every=1,
    )

    assert ticks == [1, 0]
    assert coord.get_watermark(hunt) == fixed_now  # unchanged by the second tick
