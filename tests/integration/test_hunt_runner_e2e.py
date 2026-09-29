#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_runner_e2e.py
#  Purpose:      End-to-end hunt-runner over REAL ClickHouse only (no Postgres)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Full hunt loop over real ClickHouse alone (no Postgres, no mocks).

Proves the runtime on CH only: the worker runs a windowed INSERT and advances the
watermark so the next window resumes incrementally (no duplicate rows), and a
runner tick claims + runs a due hunt while NEVER double-running one that is already
leased (records the too-aggressive overrun instead). Uses the ClickHouse the .env
configures, else a docker ClickHouse, in the ``dfe_db`` database that carries the
coordination tables the schema phase applies.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import pytest
from common.hunt_files import write_hunt, write_rule

from dfe_engine.hunt_runner import (
    ChCoordinator,
    HuntRunner,
    HuntSpec,
    HuntWorker,
    load_specs,
    read_run_status,
)
from dfe_engine.hunt_runner.ch_coordinator import Lease
from dfe_engine.hunt_runner.spread import current_fire


@pytest.fixture
def ch_db(ch_client, dfe_db):
    """``dfe_db`` plus the scratch src/tgt tables the direct queries here read and write."""
    ch_client.command(
        f"CREATE TABLE `{dfe_db}`.src (timestamp_load Int64, ev String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(f"CREATE TABLE `{dfe_db}`.tgt (ev String) ENGINE = MergeTree ORDER BY ev")
    return dfe_db


def _count(ch, table: str) -> int:
    return int(ch.query(f"SELECT count() FROM {table}").result_rows[0][0])


def _coord(ch, db, worker_id="w1"):
    return ChCoordinator(
        ch, database=db, worker_id=worker_id, settle_seconds=0.0, sleep=lambda _s: None
    )


def test_worker_executes_and_resumes_incrementally(ch_client, ch_db):
    ch_client.command(
        f"INSERT INTO `{ch_db}`.src (timestamp_load, ev) VALUES (500,'a'),(900,'b'),(1200,'c')"
    )
    coord = _coord(ch_client, ch_db)
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)
    spec = HuntSpec(
        hunt_id="h1",
        interval_seconds=600,
        queries=[f"INSERT INTO `{ch_db}`.tgt SELECT ev FROM `{ch_db}`.src WHERE {{window}}"],
        timestamp_field="timestamp_load",
    )

    # first window [400,1000) -> a,b (not c@1200)
    assert worker.run(spec, scheduled_start=1000) == 1000
    assert _count(ch_client, f"`{ch_db}`.tgt") == 2
    # next window [1000,1300) -> only c; a,b are NOT re-run
    assert worker.run(spec, scheduled_start=1300) == 1300
    assert _count(ch_client, f"`{ch_db}`.tgt") == 3  # 2 + 1, no duplicates
    assert coord.get_watermark("h1") == 1300


def test_tick_runs_due_hunt_and_frees_slot(ch_client, ch_db):
    ch_client.command(f"INSERT INTO `{ch_db}`.src (timestamp_load, ev) VALUES (10,'x'),(20,'y')")
    coord = _coord(ch_client, ch_db)
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)
    spec = HuntSpec(
        hunt_id="h2",
        interval_seconds=600,
        queries=[f"INSERT INTO `{ch_db}`.tgt SELECT ev FROM `{ch_db}`.src WHERE {{window}}"],
        timestamp_field="timestamp_load",
    )
    runner = HuntRunner(coord, worker, {spec.hunt_id: spec}, cap=4)

    fire = current_fire("h2", 600, 5000)
    now = fire + 1  # guaranteed due (now >= this interval's fire)
    assert runner.tick(now) == 1
    assert coord.active_count(now) == 0  # slot released after the run
    # a second tick in the SAME interval must not re-run (fire already completed)
    assert runner.tick(now) == 0


def test_tick_never_double_runs_a_leased_hunt(ch_client, ch_db):
    coord = _coord(ch_client, ch_db, "runner")
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)
    spec = HuntSpec(hunt_id="h3", interval_seconds=600)  # never reached: the lease blocks it
    runner = HuntRunner(coord, worker, {spec.hunt_id: spec}, cap=4)

    fire = current_fire("h3", 600, 5000)
    now = fire + 1
    # a DIFFERENT worker holds an active lease (a prior fire still running)
    other = _coord(ch_client, ch_db, "other")
    assert other.try_claim("h3", fire=fire, now=now) is True
    # the runner must NOT double-run it - it records an overrun and skips
    assert runner.tick(now) == 0
    state = coord.get_state("h3")
    assert state is not None
    assert state.too_aggressive is True


class _PeerFinishesFirst(ChCoordinator):
    """A real coordinator that lets a peer runner finish the fire just before it reads the lease.

    The tick reads the watermark, then the lease. Nothing injectable sits between the two,
    so this is where a second runner's whole run is put.
    """

    def __init__(self, *args, before_first_lease_read: Callable[[], None], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._before_first_lease_read: Callable[[], None] | None = before_first_lease_read

    def current_lease(self, hunt_id: str) -> Lease | None:
        hook, self._before_first_lease_read = self._before_first_lease_read, None
        if hook is not None:
            hook()
        return super().current_lease(hunt_id)


def test_a_fire_a_peer_finishes_mid_tick_is_not_run_again(ch_client, dfe_db, tmp_path):
    """Runner B reads the watermark, runner A runs and releases the fire, then B claims it.

    B wins that claim fairly: the lease it sees is released. Only a second look at the
    watermark stops B running the fire again and overwriting its run record with an
    empty window's 0 rows.
    """
    hunt = f"race_{uuid.uuid4().hex[:8]}"
    rule_id = f"{hunt}_rule"
    org = f"org-{uuid.uuid4().hex[:8]}"
    write_rule(tmp_path / "rules", rule_id, f"_org_id = '{org}'")
    write_hunt(tmp_path / "hunts", hunt, rule_id, dfe_db)
    specs = load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules")

    fire = current_fire(hunt, 60, int(time.time()))
    now = fire + 1
    ch_client.command(
        f"INSERT INTO `{dfe_db}`.`main` (_timestamp_load, _timestamp, _org_id) "
        f"SELECT toDateTime64({fire} - 1 - number, 3), toDateTime64({fire} - 1 - number, 3), "
        f"'{org}' FROM numbers(3)"
    )

    coord_a = _coord(ch_client, dfe_db, "runner-a")
    coord_a.ensure_schema()
    runner_a = HuntRunner(coord_a, HuntWorker(ch_client, coord_a), specs, cap=8)
    ran_a: list[int] = []
    coord_b = _PeerFinishesFirst(
        ch_client,
        database=dfe_db,
        worker_id="runner-b",
        settle_seconds=0.0,
        sleep=lambda _s: None,
        before_first_lease_read=lambda: ran_a.append(runner_a.tick(now)),
    )
    runner_b = HuntRunner(coord_b, HuntWorker(ch_client, coord_b), specs, cap=8)

    assert runner_b.tick(now) == 0
    assert ran_a == [1]
    assert coord_b.active_count(now) == 0  # B released the claim it did not use
    assert read_run_status(ch_client, dfe_db, [hunt], now=now)[hunt].last_run_rows == 3
    detections = ch_client.query(
        f"SELECT count() FROM `{dfe_db}`.detection WHERE hunt_name = {{h:String}}",
        parameters={"h": hunt},
    ).result_rows[0][0]
    assert detections == 3
