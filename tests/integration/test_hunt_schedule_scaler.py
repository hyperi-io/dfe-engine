#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_schedule_scaler.py
#  Purpose:      Live-CH proof the deterministic-due scaler query counts reality
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The KEDA scaler query against real ClickHouse (no mocks).

Proves ``due_query`` (run via ``due_count``) implements the parity predicate the
unit test pins: it counts exactly the hunts a worker would run - due, not already
completed at this fire, and unclaimed - and it EXCLUDES tombstoned (deleted) hunts,
those with a future watermark (fire already done), and those holding an active
lease. Determinism against the server's real now() is achieved with interval=2,
offset=0 hunts (fire = now//2*2 <= now, so always due) and forced not-due cases.
The schedule and coordination tables are the ones the schema phase applies, from
the ``dfe_db`` fixture.
"""

from __future__ import annotations

from pathlib import Path

from dfe_engine.hunts import runner_cli
from dfe_engine.hunts.ch_coordinator import ChCoordinator
from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.hunts.schedule import (
    due_count,
    phase_offset,
    publish_schedule,
    read_schedule,
    write_offsets,
)
from dfe_engine.hunts.spec_loader import load_specs
from dfe_engine.settings import DFESettings, HuntSmoothingSettings, HuntsSettings


def _spec(hunt_id: str, interval: int = 2) -> HuntSpec:
    # interval=2 -> phase offset 0, so the current fire (now//2*2) is always <= now
    return HuntSpec(hunt_id=hunt_id, interval_seconds=interval)


def test_due_count_counts_exactly_the_runnable_hunts(ch_client, dfe_db):
    coord = ChCoordinator(ch_client, database=dfe_db, sleep=lambda _s: None, monotonic=lambda: 0.0)
    coord.ensure_schema()  # hunt_lease / hunt_watermark / hunt_state (the LEFT JOINs)

    server_now = int(ch_client.query("SELECT toInt64(now())").result_rows[0][0])

    specs = {h: _spec(h) for h in ("due1", "due2", "notdue_wm", "notdue_lease", "gone")}
    assert publish_schedule(ch_client, dfe_db, specs) == 5

    # notdue_wm: watermark far in the future -> this fire already "done" -> NOT due
    coord.set_watermark("notdue_wm", server_now + 10**9)
    # notdue_lease: an active lease (lease_until = now + 300) -> running -> NOT due
    assert coord.try_claim("notdue_lease", fire=server_now, now=server_now) is True
    # 'gone' is removed from config and re-published -> tombstoned (enabled=0)
    del specs["gone"]
    publish_schedule(ch_client, dfe_db, specs)

    # Only due1 + due2 are due-and-unclaimed. gone is tombstoned, notdue_wm/lease excluded.
    assert due_count(ch_client, dfe_db) == 2


def test_due_count_is_zero_when_all_caught_up(ch_client, dfe_db):
    """Scale-to-zero: when every hunt's fire is completed, the backlog is 0."""
    coord = ChCoordinator(ch_client, database=dfe_db, sleep=lambda _s: None, monotonic=lambda: 0.0)
    coord.ensure_schema()
    server_now = int(ch_client.query("SELECT toInt64(now())").result_rows[0][0])

    specs = {h: _spec(h) for h in ("h1", "h2", "h3")}
    publish_schedule(ch_client, dfe_db, specs)
    # advance every watermark past any plausible current fire -> nothing owed
    for h in specs:
        coord.set_watermark(h, server_now + 10**9)

    assert due_count(ch_client, dfe_db) == 0


def test_a_hunt_removed_then_added_back_is_scheduled_again(ch_client, dfe_db):
    """The re-add writes the same block as the first publish, and it has to land.

    A Replicated table drops an insert identical to a recent one, and ``updated`` is
    filled in by the server, so without the insert settings the tombstone wins. A hunt
    disabled and enabled again leaves and rejoins the specs, so it is this sequence.
    """
    coord = ChCoordinator(ch_client, database=dfe_db, sleep=lambda _s: None, monotonic=lambda: 0.0)
    coord.ensure_schema()
    specs = {h: _spec(h) for h in ("kept", "back")}
    publish_schedule(ch_client, dfe_db, specs)
    publish_schedule(ch_client, dfe_db, {"kept": specs["kept"]})
    assert due_count(ch_client, dfe_db) == 1

    publish_schedule(ch_client, dfe_db, specs)

    enabled = ch_client.query(
        f"SELECT argMax(enabled, updated) FROM `{dfe_db}`.hunt_schedule WHERE hunt_id = 'back'"
    ).result_rows[0][0]
    assert enabled == 1, "the re-added hunt stayed tombstoned"
    assert due_count(ch_client, dfe_db) == 2


def _hunt(hunt_dir: Path, name: str, *, interval: str = "2s", enabled: bool = True) -> None:
    (hunt_dir / f"{name}.yaml").write_text(
        f'schedule:\n  interval: {interval}\nquery: "SELECT 1 WHERE {{window}}"\n'
        f"enabled: {str(enabled).lower()}\n"
    )


def _schedule_rows(ch_client, dfe_db) -> int:
    return int(ch_client.query(f"SELECT count() FROM `{dfe_db}`.hunt_schedule").result_rows[0][0])


def test_a_running_runner_keeps_the_schedule_current_for_keda(ch_client, dfe_db, tmp_path: Path):
    """The runner's own publish is the only writer on Kubernetes, so each reload must leave KEDA right."""
    coord = ChCoordinator(ch_client, database=dfe_db, sleep=lambda _s: None, monotonic=lambda: 0.0)
    coord.ensure_schema()
    settings = DFESettings(env="test")
    hunt_dir = tmp_path / "hunts"
    hunt_dir.mkdir()

    publisher = runner_cli._SchedulePublisher(ch_client, dfe_db, settings)

    def reload() -> None:
        publisher.publish(load_specs(hunt_dir))

    # A runner starts on an empty table: KEDA can see both hunts with no worker running.
    _hunt(hunt_dir, "a")
    _hunt(hunt_dir, "b")
    reload()
    assert set(read_schedule(ch_client, dfe_db)) == {"a", "b"}
    assert due_count(ch_client, dfe_db) == 2

    # A hunt added in git is scheduled at the next reload.
    _hunt(hunt_dir, "c")
    reload()
    assert set(read_schedule(ch_client, dfe_db)) == {"a", "b", "c"}
    assert due_count(ch_client, dfe_db) == 3

    # One deleted and one disabled stop counting.
    (hunt_dir / "a.yaml").unlink()
    _hunt(hunt_dir, "b", enabled=False)
    reload()
    assert set(read_schedule(ch_client, dfe_db)) == {"c"}
    assert due_count(ch_client, dfe_db) == 1

    # A changed interval is rewritten, and a reload with nothing changed writes nothing.
    _hunt(hunt_dir, "c", interval="4s")
    reload()
    assert read_schedule(ch_client, dfe_db)["c"][0] == 4
    rows = _schedule_rows(ch_client, dfe_db)
    reload()
    assert _schedule_rows(ch_client, dfe_db) == rows


def test_a_runner_with_smoothing_on_keeps_the_offset_the_smoother_learned(
    ch_client, dfe_db, tmp_path: Path
):
    coord = ChCoordinator(ch_client, database=dfe_db, sleep=lambda _s: None, monotonic=lambda: 0.0)
    coord.ensure_schema()
    smoothing = HuntsSettings(smoothing=HuntSmoothingSettings(enabled=True))
    settings = DFESettings(env="test", hunts=smoothing)
    hunt_dir = tmp_path / "hunts"
    hunt_dir.mkdir()
    publisher = runner_cli._SchedulePublisher(ch_client, dfe_db, settings)
    _hunt(hunt_dir, "a", interval="10m")
    publisher.publish(load_specs(hunt_dir))
    learned = phase_offset("a", 600) + 17
    write_offsets(ch_client, dfe_db, {"a": (600, learned)})

    _hunt(hunt_dir, "b", interval="10m")
    publisher.publish(load_specs(hunt_dir))

    schedule = read_schedule(ch_client, dfe_db)
    assert schedule["a"] == (600, learned)
    assert schedule["b"] == (600, phase_offset("b", 600))
