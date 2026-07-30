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
races (the no-timing-flake rule).
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


def _shares_db_across_concurrent_connections(ch_client, ch_params) -> bool:
    """True if a plain CREATE DATABASE is visible from CONCURRENT fresh connections.

    The multi-pod test opens one connection PER pod. On a multi-node CH cluster behind
    a round-robin load balancer, a plain (non-Replicated) database lands on ONE node,
    so concurrent connections spread across the OTHER nodes get UNKNOWN_DATABASE. The
    hunt coordinator therefore needs a single logical ClickHouse - a single node, or
    Replicated coordination tables - on the clustered 'scale' tier (see
    docs/data-plane/hunt-runner-scaling.md). This probes that premise so the test skips DETERMINISTICALLY
    where it cannot hold instead of failing flakily. It must be CONCURRENT: sequential
    connections often stick to one node and hide the split.
    """
    import concurrent.futures

    import clickhouse_connect

    probe = f"dfe_probe_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{probe}`")

    def _sees(_i: int) -> int:
        c = clickhouse_connect.get_client(**ch_params)
        try:
            return int(
                c.query(f"SELECT count() FROM system.databases WHERE name = '{probe}'").result_rows[
                    0
                ][0]
            )
        except Exception:
            return 0
        finally:
            c.close()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_sees, range(8)))
        return all(r == 1 for r in results)
    finally:
        try:
            ch_client.command(f"DROP DATABASE IF EXISTS `{probe}`")
        except Exception:
            pass


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

    # This test needs one connection PER pod to share coordination state. On a
    # multi-node cluster behind a round-robin LB a plain database is not shared across
    # concurrent connections, so skip DETERMINISTICALLY where the premise cannot hold
    # (rather than fail flakily) - it runs on single-node tiers.
    if not _shares_db_across_concurrent_connections(ch_client, ch_params):
        pytest.skip(
            "multi-pod needs a single logical ClickHouse: this endpoint is a multi-node "
            "cluster where a plain database is not shared across concurrent connections. "
            "Scale-tier coordination must use Replicated tables or a single CH endpoint "
            "(see docs/data-plane/hunt-runner-scaling.md)."
        )

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
    hids = list(specs)
    # Round 1 makes distribution DETERMINISTIC (not a latency race): each pod claims
    # its OWN distinct hunt and waits at the barrier, so all N claims are held before
    # anyone releases -> N pods provably do work. (A free-running loop cannot promise
    # this: try_claim is "latest claim wins", so one fast pod legitimately cascades
    # through and does everything - valid, but NOT a property to assert.) Round 2 is
    # then a single BOUNDED pass per pod over every hunt: every pod passes every hunt,
    # so each remaining hunt is claimed+run by exactly one pod, and a single pass always
    # terminates - no free-running loop that could zombie past the fixture's DROP
    # DATABASE. Daemon threads are the backstop if a pass somehow hangs.
    start = threading.Barrier(n)

    def pod(k: int) -> None:
        wid = f"w{k}"
        client = None
        try:
            client = clickhouse_connect.get_client(**ch_params)
            coord = ChCoordinator(
                client, database=db, worker_id=wid, settle_seconds=0.2, sleep=time.sleep
            )
            worker = HuntWorker(client, coord)
            # Round 1: claim my OWN distinct hunt, hold at the barrier until all N have.
            mine = hids[k]
            coord.try_claim(mine, fires[mine], now)
            start.wait(timeout=30)
            worker.run(specs[mine], fires[mine])
            coord.release(mine, fires[mine])
            with lock:
                ran.append((mine, wid))
            # Round 2: one bounded pass, claiming any hunt not yet done or held.
            for hid in hids:
                if (coord.get_watermark(hid) or -1) >= fires[hid]:
                    continue  # already done
                lease = coord.current_lease(hid)
                if lease is not None and lease.lease_until > now:
                    continue  # another pod holds it
                if coord.try_claim(hid, fires[hid], now):
                    worker.run(specs[hid], fires[hid])
                    coord.release(hid, fires[hid])
                    with lock:
                        ran.append((hid, wid))
        finally:
            if client is not None:
                client.close()

    threads = [threading.Thread(target=pod, args=(k,), daemon=True) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "pod threads did not terminate"

    # exactly-once: every hunt ran exactly once - none lost, none double-run
    rows = ch_client.query(f"SELECT hunt_id, count() FROM `{db}`.runs GROUP BY hunt_id").result_rows
    counts = {r[0]: int(r[1]) for r in rows}
    assert len(counts) == m, f"not all hunts ran: {sorted(counts)}"
    assert all(c == 1 for c in counts.values()), f"double-run detected: {counts}"
    assert _count(ch_client, f"`{db}`.runs") == m
    # distribution is deterministic here: round 1 had all N pods each run their own
    # distinct hunt, so exactly N distinct workers did work.
    workers_used = {wid for _, wid in ran}
    assert len(workers_used) == n, f"round-1 distribution broken: {workers_used}"


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
