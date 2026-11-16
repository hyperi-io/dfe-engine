#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_smoothing.py
#  Purpose:      The smoothing beta against real ClickHouse: one offset source, costs, one leader
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hunt schedule smoothing on real ClickHouse, no mocks.

The behaviours the beta promises, each asserted on rows in ``hunt_schedule`` or the
archive rather than on timings: with the flag off the table carries exactly the
hash offsets and a restore puts them back, materialise keeps a learned offset for an
unchanged hunt and drops it for a changed interval, real fires through the worker
leave archive rows the cost model groups per fire, a cycle moves one colliding hunt
and leaves a hunt with no cost where it is, and two smoothers never lead at once.
Time is the coordinator's injected clock, so nothing here races a real second.
"""

import threading
import uuid

import pytest

from dfe_engine.clickhouse.query_log_archive import ARCHIVE_ID
from dfe_engine.hunts.ch_coordinator import ChCoordinator
from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.hunts.schedule import (
    learned_offsets,
    phase_offset,
    publish_schedule,
    read_schedule,
    read_schedule_rows,
    restore_hash_offsets,
    write_offsets,
)
from dfe_engine.hunts.smoothing import (
    LEADER_LOCK_NAME,
    PhaseSmoother,
    archive_has_cpu,
    read_costs,
    read_observed,
)
from dfe_engine.hunts.worker import HuntWorker
from dfe_engine.schema.lock import SchemaLock
from dfe_engine.schema.plan import LOCK_ID, object_names, render_one
from dfe_engine.settings import HuntSmoothingSettings


@pytest.fixture
def smoothing_db(ch_client, dfe_db):
    """The coordination database plus the schema lock table and the query archive.

    The archive's view reads ``system.query_log``, which ClickHouse creates on its
    first flush, so the flush comes first.
    """
    from dfe_schemas.clickhouse import PlatformProbe

    platform = PlatformProbe(ch_client).platform
    ch_client.command("SYSTEM FLUSH LOGS")
    for object_id in (LOCK_ID, ARCHIVE_ID, f"{ARCHIVE_ID}_mv"):
        for statement in render_one(object_id, data_database=dfe_db, platform=platform).statements:
            ch_client.command(statement)
    return dfe_db


def _settings(**overrides) -> HuntSmoothingSettings:
    base = {
        "enabled": True,
        "cycle_seconds": 300.0,
        "cooldown_cycles": 0,
        "window_days": 1,
        "hysteresis": 0.0,
    }
    base.update(overrides)
    return HuntSmoothingSettings(**base)


def _smoother(ch, db, *, holder: str, **overrides) -> PhaseSmoother:
    return PhaseSmoother(
        ch,
        db,
        lock_table=object_names(LOCK_ID, data_database=db)[LOCK_ID],
        settings=_settings(**overrides),
        fraction=0.8,
        holder=holder,
    )


def _specs(*hunt_ids: str, interval: int = 600) -> dict[str, HuntSpec]:
    return {h: HuntSpec(hunt_id=h, interval_seconds=interval) for h in hunt_ids}


def test_with_smoothing_off_the_table_holds_exactly_the_hash_offsets_and_a_restore_returns_to_them(
    ch_client, smoothing_db
):
    specs = _specs("off_a", "off_b")
    publish_schedule(ch_client, smoothing_db, specs)
    hashed = {h: (600, phase_offset(h, 600)) for h in specs}
    assert read_schedule(ch_client, smoothing_db) == hashed

    # A past run learned an offset. Off restores the hash, and only that row is written.
    write_offsets(ch_client, smoothing_db, {"off_a": (600, phase_offset("off_a", 600) + 11)})
    assert read_schedule(ch_client, smoothing_db)["off_a"] != hashed["off_a"]
    assert restore_hash_offsets(ch_client, smoothing_db) == 1
    assert read_schedule(ch_client, smoothing_db) == hashed
    assert restore_hash_offsets(ch_client, smoothing_db) == 0


@pytest.mark.parametrize(
    ("new_interval", "keeps"),
    [
        pytest.param(600, True, id="unchanged_interval_keeps"),
        pytest.param(900, False, id="changed_interval_drops"),
    ],
)
def test_materialise_keeps_a_learned_offset_only_while_the_interval_is_unchanged(
    ch_client, smoothing_db, new_interval, keeps
):
    specs = _specs("mat_a", "mat_b")
    publish_schedule(ch_client, smoothing_db, specs)
    learned = phase_offset("mat_a", 600) + 17
    write_offsets(ch_client, smoothing_db, {"mat_a": (600, learned)})

    changed = _specs("mat_b") | _specs("mat_a", interval=new_interval)
    kept = learned_offsets(read_schedule(ch_client, smoothing_db), changed)
    publish_schedule(ch_client, smoothing_db, changed, offsets=kept)

    want = learned if keeps else phase_offset("mat_a", new_interval)
    assert read_schedule(ch_client, smoothing_db)["mat_a"] == (new_interval, want)


def test_real_fires_leave_archive_rows_the_cost_model_groups_per_fire(ch_client, smoothing_db):
    db = smoothing_db
    hunt_id = f"cost_{uuid.uuid4().hex[:8]}"
    ch_client.command(
        f"CREATE TABLE `{db}`.cost_src (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.command(
        f"CREATE TABLE `{db}`.cost_tgt (timestamp_load Int64, msg String) "
        "ENGINE = MergeTree ORDER BY timestamp_load"
    )
    ch_client.insert(
        f"{db}.cost_src",
        [[100, "a"], [150, "b"], [250, "c"]],
        column_names=["timestamp_load", "msg"],
    )
    spec = HuntSpec(
        hunt_id=hunt_id,
        interval_seconds=60,
        queries=[
            f"INSERT INTO `{db}`.cost_tgt SELECT timestamp_load, msg FROM `{db}`.cost_src WHERE {{window}}"
        ],
        timestamp_field="timestamp_load",
    )
    coord = ChCoordinator(ch_client, database=db, sleep=lambda _s: None, monotonic=lambda: 0.0)
    coord.ensure_schema()
    coord.set_watermark(hunt_id, 100)
    worker = HuntWorker(ch_client, coord)
    for fire in (200, 260, 320):
        worker.run(spec, scheduled_start=fire)
    ch_client.command("SYSTEM FLUSH LOGS")

    assert archive_has_cpu(ch_client, db) is False
    costs = read_costs(ch_client, db, window_days=1, quantile=0.95, with_cpu=False)
    assert hunt_id in costs
    cost = costs[hunt_id]
    assert cost.fires == 3
    assert cost.mem_bytes >= 0
    assert cost.duration_ms >= 0
    assert cost.cpu_us is None
    # A hunt that never fired has no row, so the smoother leaves it on its offset.
    assert "never_fired" not in costs
    overlap, mem = read_observed(ch_client, db, bucket_seconds=30)
    assert overlap >= 1
    assert mem >= 0


def test_a_cycle_moves_one_colliding_hunt_and_leaves_a_hunt_with_no_cost_alone(
    ch_client, smoothing_db
):
    db = smoothing_db
    tag = uuid.uuid4().hex[:6]
    a, b, quiet = f"col_a_{tag}", f"col_b_{tag}", f"col_quiet_{tag}"
    specs = _specs(a, b, quiet)
    publish_schedule(ch_client, db, specs)
    # Force a collision: both costed hunts on the same learned offset.
    write_offsets(ch_client, db, {a: (600, 60), b: (600, 60)})
    # Real fires give both a cost row, the quiet hunt gets none.
    coord = ChCoordinator(ch_client, database=db, sleep=lambda _s: None, monotonic=lambda: 0.0)
    coord.ensure_schema()
    worker = HuntWorker(ch_client, coord)
    for hunt in (a, b):
        spec = HuntSpec(
            hunt_id=hunt,
            interval_seconds=600,
            queries=["SELECT count() FROM numbers(1000) WHERE 1"],
        )
        coord.set_watermark(hunt, 0)
        worker.run(spec, scheduled_start=600)
    ch_client.command("SYSTEM FLUSH LOGS")

    before = read_schedule_rows(ch_client, db)
    move = _smoother(ch_client, db, holder="one").cycle(1_800_000_000, specs)

    assert move is not None
    assert move.hunt_id in {a, b}
    assert move.old_offset == 60
    assert 0 <= move.new_offset < 480
    assert abs(move.new_offset - 60) <= 60
    after = read_schedule_rows(ch_client, db)
    assert after[move.hunt_id].phase_offset == move.new_offset
    other = b if move.hunt_id == a else a
    assert after[other].phase_offset == 60
    assert after[quiet] == before[quiet]
    assert after[other].updated == before[other].updated


def test_a_cycle_tombstones_a_hunt_removed_from_the_specs(ch_client, smoothing_db):
    db = smoothing_db
    tag = uuid.uuid4().hex[:6]
    kept, gone = f"keep_{tag}", f"gone_{tag}"
    publish_schedule(ch_client, db, _specs(kept, gone))
    assert set(read_schedule(ch_client, db)) >= {kept, gone}

    _smoother(ch_client, db, holder="one").cycle(1_800_000_000, _specs(kept))

    schedule = read_schedule(ch_client, db)
    assert kept in schedule
    assert gone not in schedule, "a removed hunt stayed enabled, so KEDA would count it forever"


def test_the_fleet_runs_at_most_one_cycle_per_window_whoever_leads(ch_client, smoothing_db):
    """The lease lasts a cycle, so a second runner ticking seconds later does not lead."""
    db = smoothing_db
    specs = _specs(f"pace_{uuid.uuid4().hex[:6]}")
    first = _smoother(ch_client, db, holder="runner-a", cycle_seconds=4.0)
    second = _smoother(ch_client, db, holder="runner-b", cycle_seconds=4.0)
    now = 1_800_000_000

    assert first.maybe_cycle(now, specs) is True
    assert second.maybe_cycle(now + 1, specs) is False

    # The first runner's own next attempt inside the window is held as well.
    first._next_cycle = 0.0
    assert first.maybe_cycle(now + 2, specs) is False

    # Once the lease has expired on the server's clock, the window is open again.
    import time

    led = False
    deadline = time.monotonic() + 30
    while not led and time.monotonic() < deadline:
        led = second.maybe_cycle(now + 5, specs)
        if not led:
            time.sleep(0.5)
    assert led is True


def test_two_smoothers_never_both_lead_a_cycle(ch_client, ch_params, smoothing_db):
    import clickhouse_connect

    db = smoothing_db
    table = object_names(LOCK_ID, data_database=db)[LOCK_ID]
    locks = [
        SchemaLock(
            clickhouse_connect.get_client(**ch_params),
            database=db,
            table=table,
            holder=f"runner-{i}",
            lease_seconds=60,
            lock_name=LEADER_LOCK_NAME,
        )
        for i in range(2)
    ]
    won: list[bool] = []
    threads = [threading.Thread(target=lambda lk=lk: won.append(lk.acquire())) for lk in locks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(won) == [False, True]
    for lk in locks:
        lk.release_or_lapse()

    # Once the leader has released, the next cycle is open to either.
    assert locks[0].acquire() is True
    assert locks[1].acquire() is False
    locks[0].release()
