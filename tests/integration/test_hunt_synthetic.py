#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_synthetic.py
#  Purpose:      Synthetic hunt-runner distribution tests against real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Emulated synthetic distribution tests for the hunt runner (no mocks).

  - scheduling cadence + incremental resume over stepped simulated time,
  - multi-pod competing-consumers: N real threads (one CH client each) claim M
    hunts and each runs EXACTLY once, with work genuinely shared,
  - the global concurrency cap protecting ClickHouse.

Uses the tiered CH harness (cluster / remote docker / local); isolated db, dropped
after. Time is deterministic via the coordinator's injected clock - no wall-clock
races (per Derek's "no timing flake" rule).
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from dfe_engine.hunt_runner import ChCoordinator, HuntRunner, HuntSpec, HuntWorker
from dfe_engine.hunt_runner.spread import current_fire


@pytest.fixture
def synth_db(ch_client):
    db = f"dfe_synth_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{db}`")
    try:
        yield db
    finally:
        try:
            ch_client.command(f"DROP DATABASE IF EXISTS `{db}`")
        except Exception:
            pass


def _count(ch, table: str) -> int:
    return int(ch.query(f"SELECT count() FROM {table}").result_rows[0][0])


def test_scheduling_cadence_and_incremental_resume(ch_client, synth_db):
    """A hunt fires exactly once per interval and resumes across 3 windows, no dup."""
    db = synth_db
    ch_client.command(
        f"CREATE TABLE `{db}`.src (timestamp_load Int64, ev String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(f"CREATE TABLE `{db}`.tgt (ev String) ENGINE = MergeTree ORDER BY ev")
    coord = ChCoordinator(
        ch_client, database=db, worker_id="w", settle_seconds=0.0, sleep=lambda _s: None
    )
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)
    spec = HuntSpec(
        hunt_id="h",
        interval_seconds=600,
        query=f"INSERT INTO `{db}`.tgt SELECT ev FROM `{db}`.src WHERE {{window}}",
    )
    runner = HuntRunner(coord, worker, {spec.hunt_id: spec}, cap=4)

    interval = 600
    f1 = current_fire("h", interval, 1_000_000)
    fires = [f1, f1 + interval, f1 + 2 * interval]
    # one src row per window: W1=[f1-600,f1), W2=[f1,f1+600), W3=[f1+600,f1+1200)
    ch_client.command(
        f"INSERT INTO `{db}`.src (timestamp_load, ev) VALUES "
        f"({f1 - 300},'a'),({f1 + 300},'b'),({f1 + 900},'c')"
    )
    # step the clock interval by interval - the hunt fires once each time
    for fk in fires:
        assert runner.tick(fk + 1) == 1
    assert _count(ch_client, f"`{db}`.tgt") == 3  # a,b,c - each window once, no dup
    assert coord.get_watermark("h") == fires[-1]
    # a second tick in the last interval must not re-run (fire already completed)
    assert runner.tick(fires[-1] + 1) == 0
    assert _count(ch_client, f"`{db}`.tgt") == 3


def test_multi_pod_exactly_once_and_distributed(ch_params, ch_client, synth_db):
    """N competing pods claim M hunts: each runs exactly once and work is shared."""
    import clickhouse_connect

    db = synth_db
    ch_client.command(
        f"CREATE TABLE `{db}`.runs (hunt_id String, run_at DateTime64(3) DEFAULT now64(3)) "
        "ENGINE = MergeTree ORDER BY hunt_id"
    )
    ChCoordinator(ch_client, database=db, worker_id="init").ensure_schema()

    m, n, now = 15, 4, 1_000_000
    specs = {
        f"h{i}": HuntSpec(
            hunt_id=f"h{i}",
            interval_seconds=600,
            query=f"INSERT INTO `{db}`.runs (hunt_id) VALUES ('h{i}')",
        )
        for i in range(m)
    }
    fires = {hid: current_fire(hid, 600, now) for hid in specs}

    ran: list[tuple[str, str]] = []  # (hunt_id, worker_id) - who ran what
    lock = threading.Lock()

    def pod(wid: str) -> None:
        client = clickhouse_connect.get_client(**ch_params)
        coord = ChCoordinator(
            client, database=db, worker_id=wid, settle_seconds=0.2, sleep=time.sleep
        )
        worker = HuntWorker(client, coord)
        idle = 0
        while idle < 300:
            did = False
            for hid, spec in specs.items():
                fire = fires[hid]
                wm = coord.get_watermark(hid)
                if wm is not None and wm >= fire:
                    continue  # this hunt already done
                lease = coord.current_lease(hid)
                if lease is not None and lease.lease_until > now:
                    continue  # another pod holds it
                if coord.try_claim(hid, fire, now):
                    worker.run(spec, fire)  # inserts the marker + advances watermark
                    coord.release(hid, fire)
                    with lock:
                        ran.append((hid, wid))
                    did = True
                    break  # one at a time -> yield to peers (competing consumers)
            if not did:
                if all((coord.get_watermark(h) or -1) >= fires[h] for h in specs):  # all complete
                    return
                idle += 1
                time.sleep(0.02)

    threads = [threading.Thread(target=pod, args=(f"w{k}",)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)

    # exactly-once: every hunt ran exactly once - none lost, none double-run
    rows = ch_client.query(f"SELECT hunt_id, count() FROM `{db}`.runs GROUP BY hunt_id").result_rows
    counts = {r[0]: int(r[1]) for r in rows}
    assert len(counts) == m, f"not all hunts ran: {sorted(counts)}"
    assert all(c == 1 for c in counts.values()), f"double-run detected: {counts}"
    assert _count(ch_client, f"`{db}`.runs") == m
    # genuinely distributed: more than one pod did work
    workers_used = {wid for _, wid in ran}
    assert len(workers_used) >= 2, f"work not distributed across pods: {workers_used}"


def test_global_cap_is_never_exceeded(ch_client, synth_db):
    """The runner runs nothing when the global cap is already saturated."""
    db = synth_db
    coord = ChCoordinator(
        ch_client, database=db, worker_id="w", settle_seconds=0.0, sleep=lambda _s: None
    )
    coord.ensure_schema()
    other = ChCoordinator(
        ch_client,
        database=db,
        worker_id="other",
        lease_seconds=100_000,
        settle_seconds=0.0,
        sleep=lambda _s: None,
    )
    now, cap = 1_000_000, 3
    # another worker holds `cap` active leases -> the pool is saturated
    for i in range(cap):
        assert other.try_claim(f"busy{i}", fire=1000, now=now) is True
    assert coord.active_count(now) == cap

    specs = {f"n{i}": HuntSpec(hunt_id=f"n{i}", interval_seconds=1, query="") for i in range(5)}
    runner = HuntRunner(coord, HuntWorker(ch_client, coord), specs, cap=cap)
    assert runner.tick(now) == 0  # at cap -> nothing runs (protects ClickHouse)
