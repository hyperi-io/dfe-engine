#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_worker_safe_horizon.py
#  Purpose:      The safe horizon hold, the overlap read and the dedupe conjunct a fire binds
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a fire does with a lagging replica, and what it reads once it runs.

The real exactly-once behaviour is proved on three ClickHouse replicas in
``tests/integration/test_hunt_safe_horizon.py``. These pin the decisions in between:
when a fire is held, when the cap lets it go, and the window each statement binds.
"""

from types import SimpleNamespace

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from dfe_engine.hunts.hunt_spec import HuntSpec, HuntStatement
from dfe_engine.hunts.runner_metrics import HuntRunnerMetrics
from dfe_engine.hunts.worker import HuntWorker
from tests.support.recording_metrics import Observation

RULE = HuntStatement(
    sql="INSERT INTO `d`.`det` SELECT _uuid FROM `d`.`src` WHERE {window} AND {unseen}",
    rule_id="r1",
    source=("d", "src"),
    target=("d", "det"),
)
RULES_HUNT = HuntSpec(hunt_id="h", interval_seconds=600, queries=[RULE], customers=["acme"])
DIRECT_HUNT = HuntSpec(
    hunt_id="direct",
    interval_seconds=600,
    queries=["INSERT INTO d.det SELECT * FROM d.src WHERE {window}"],
)


class _Ch:
    """A ClickHouse that reports one replication delay and records every statement."""

    def __init__(
        self, *, now: int, delay: int = 0, broken: bool = False, target_delay: int = 0
    ) -> None:
        self.now = now
        self.delay = delay
        self.target_delay = target_delay
        self.broken = broken
        self.queries: list[str] = []
        self.commands: list[str] = []

    def query(self, sql: str, settings=None) -> SimpleNamespace:
        self.queries.append(sql)
        if "system.replicas" in sql:
            if self.broken:
                raise DatabaseError("Code: 497. ACCESS_DENIED")
            return SimpleNamespace(result_rows=[(self.now, self.delay, self.target_delay)])
        return SimpleNamespace(result_rows=[(self.now * 1000, 0, 0)])

    def command(self, sql: str, settings=None) -> SimpleNamespace:
        self.commands.append(sql)
        return SimpleNamespace(written_rows=0)


class _Coordinator:
    """The watermark and the safe horizon marks, kept the way the ClickHouse coordinator keeps them."""

    def __init__(self, watermark: int | None, **marks: int) -> None:
        self.watermark = watermark
        self.marks: dict[str, int] = dict(marks)
        # The one write that fails, by mark name or "watermark", to stand in for a crash there.
        self.fail_on = ""

    def get_watermark(self, hunt_id: str) -> int | None:
        return self.watermark

    def set_watermark(self, hunt_id: str, watermark: int) -> None:
        if self.fail_on == "watermark":
            raise DatabaseError("Code: 210. NETWORK_ERROR")
        self.watermark = watermark

    def get_safe_horizon_mark(self, hunt_id: str, mark: str) -> int | None:
        return self.marks.get(mark) or None

    def set_safe_horizon_mark(self, hunt_id: str, mark: str, value: int) -> None:
        if self.fail_on == mark:
            raise DatabaseError("Code: 210. NETWORK_ERROR")
        self.marks[mark] = value

    def record_run(self, hunt_id: str, fire: int, rows_written: int) -> None:
        pass


def _worker(
    ch: _Ch, *, manager=None, watermark: int | None = None, coord=None, **kwargs
) -> HuntWorker:
    metrics = HuntRunnerMetrics(manager) if manager is not None else None
    coord = coord if coord is not None else _Coordinator(watermark)
    return HuntWorker(ch, coord, metrics=metrics, **kwargs)  # ty: ignore[invalid-argument-type]  # stand-ins carrying only the calls this path makes


@pytest.mark.parametrize(
    ("now", "delay", "deferred_since", "held"),
    [
        pytest.param(1000, 0, None, False, id="quiet_table"),
        pytest.param(1100, 60, None, False, id="horizon_above_fire"),
        pytest.param(1030, 60, None, True, id="horizon_below_fire_first_poll"),
        pytest.param(1299, 400, 1000, True, id="just_inside_the_cap"),
        pytest.param(1300, 400, 1000, False, id="at_the_cap"),
    ],
)
def test_a_fire_is_held_only_while_the_horizon_is_short_and_under_the_cap(
    now, delay, deferred_since, held
):
    marks = {"deferred_since": deferred_since} if deferred_since else {}
    coord = _Coordinator(None, **marks)
    worker = _worker(_Ch(now=now, delay=delay), coord=coord, max_deferral_seconds=300)

    assert worker.hold_for_safe_horizon(RULES_HUNT, 1000) is held


def test_sustained_lag_runs_at_the_cap_across_fire_boundaries(manager):
    """A 60 s hunt 93 s behind: each boundary brings a new fire, and the hold still ends."""
    short = RULES_HUNT.model_copy(update={"interval_seconds": 60})
    ch = _Ch(now=0, delay=93)
    coord = _Coordinator(None)
    worker = _worker(ch, manager=manager, coord=coord, max_deferral_seconds=300)

    held = []
    for now in range(1005, 1065, 15):
        ch.now = now
        held.append(worker.hold_for_safe_horizon(short, now // 60 * 60))
    ch.now = 1065
    held.append(worker.hold_for_safe_horizon(short, 1020))

    assert held == [True, True, True, True, False]
    assert len(manager.observed("hunt_safe_horizon_cap_reached_total")) == 1


def test_a_fire_the_cap_let_go_leaves_the_next_fire_reading_from_its_horizon():
    """Rows loaded at fire-180 that arrive after a capped fire are read by the next one."""
    coord = _Coordinator(400, deferred_since=700)
    ch = _Ch(now=1000, delay=190)
    worker = _worker(ch, coord=coord, overlap_seconds=120, max_deferral_seconds=300)

    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    worker.run(RULES_HUNT, 1000)
    # The horizon was 1000 - 190 - 10 = 800, so 820 (fire - 180) was not yet readable.
    assert coord.watermark == 1000
    assert coord.marks == {"deferred_since": 0, "safe_from": 800}

    ch.commands.clear()
    ch.now, ch.delay = 1600, 0
    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1600)
    worker.run(RULES_HUNT, 1600)

    [sql] = ch.commands
    assert "_timestamp_load >= toDateTime64(680, 3)" in sql
    assert coord.marks["safe_from"] == 0


@pytest.mark.parametrize(
    ("spec", "kwargs"),
    [
        pytest.param(DIRECT_HUNT, {}, id="direct_query_names_no_table"),
        pytest.param(RULES_HUNT, {"safe_horizon": False}, id="safe_horizon_off"),
    ],
)
def test_a_fire_with_no_safe_horizon_to_read_reads_nothing(spec, kwargs):
    ch = _Ch(now=1000, delay=900)

    assert _worker(ch, **kwargs).hold_for_safe_horizon(spec, 1000) is False
    assert ch.queries == []


@pytest.mark.parametrize(
    ("recovers_at", "safe_from", "next_read_start"),
    [
        pytest.param(None, 400, 280, id="unreadable_to_the_cap_re_reads_the_whole_window"),
        pytest.param(1120, None, 880, id="readable_again_before_the_cap"),
    ],
)
def test_an_unreadable_safe_horizon_holds_the_fire_as_unknown_lag(
    manager, recovers_at, safe_from, next_read_start
):
    """A row at 820, missing while the read fails and replicated at 1050, is still caught."""
    ch = _Ch(now=1000, broken=True)
    coord = _Coordinator(400)
    worker = _worker(
        ch,
        manager=manager,
        coord=coord,
        overlap_seconds=120,
        max_deferral_seconds=300,
        clock=lambda: ch.now,
    )

    while worker.hold_for_safe_horizon(RULES_HUNT, 1000):
        ch.now += 60
        if recovers_at is not None and ch.now >= recovers_at:
            ch.broken = False
    worker.run(RULES_HUNT, 1000)
    assert coord.watermark == 1000
    assert (coord.marks.get("safe_from") or None) == safe_from
    assert len(manager.observed("hunt_safe_horizon_unreadable_total")) >= 1

    ch.commands.clear()
    ch.now, ch.broken = 1600, False
    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1600)
    worker.run(RULES_HUNT, 1600)
    [sql] = ch.commands
    assert f"_timestamp_load >= toDateTime64({next_read_start}, 3)" in sql


def test_deferrals_are_counted_per_poll_and_per_fire(manager):
    ch = _Ch(now=1010, delay=60)
    worker = _worker(ch, manager=manager)
    assert worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    assert worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    ch.now, ch.delay = 1100, 0

    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    assert len(manager.observed("hunt_safe_horizon_deferrals_total")) == 2
    assert manager.observed("hunt_safe_horizon_deferrals_per_fire") == [
        Observation("hunt_safe_horizon_deferrals_per_fire", {"hunt_id": "h"}, "observe", 2)
    ]
    assert manager.observed("hunt_safe_horizon_cap_reached_total") == []


def test_a_fire_let_go_by_the_cap_is_counted(manager):
    coord = _Coordinator(None, deferred_since=1000)
    worker = _worker(
        _Ch(now=1300, delay=900), manager=manager, coord=coord, max_deferral_seconds=300
    )

    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    assert manager.observed("hunt_safe_horizon_cap_reached_total") == [
        Observation("hunt_safe_horizon_cap_reached_total", {"hunt_id": "h"}, "inc", 1)
    ]


@pytest.mark.parametrize(
    ("watermark", "read_start"),
    [
        pytest.param(None, 400, id="first_run_reads_its_lookback_only"),
        pytest.param(950, 650, id="resumed_run_reads_the_overlap"),
    ],
)
def test_a_rule_reads_the_overlap_and_skips_what_it_detected_over_the_same_window(
    watermark, read_start
):
    ch = _Ch(now=1000)
    _worker(ch, watermark=watermark, overlap_seconds=300).run(RULES_HUNT, 1000)

    [sql] = ch.commands
    window = (
        f"(_timestamp_load >= toDateTime64({read_start}, 3) "
        "AND _timestamp_load < toDateTime64(1000, 3))"
    )
    assert sql == (
        f"INSERT INTO `d`.`det` SELECT _uuid FROM `d`.`src` WHERE {window} AND "
        "_uuid NOT IN (SELECT matched_uuid FROM `d`.`det` "
        f"WHERE hunt_name = 'h' AND rule_id = 'r1' AND {window})"
    )


def test_a_direct_query_cannot_skip_what_it_saw_so_it_reads_no_overlap():
    ch = _Ch(now=1000)
    _worker(ch, watermark=950, overlap_seconds=300).run(DIRECT_HUNT, 1000)

    assert ch.commands == [
        "INSERT INTO d.det SELECT * FROM d.src WHERE "
        "(_timestamp_load >= toDateTime64(950, 3) AND _timestamp_load < toDateTime64(1000, 3))"
    ]


def test_the_watermark_still_advances_to_the_fire():
    coord = _Coordinator(950)
    HuntWorker(_Ch(now=1000), coord, overlap_seconds=300).run(RULES_HUNT, 1000)  # ty: ignore[invalid-argument-type]  # stand-ins carrying only the calls this path makes

    assert coord.watermark == 1000


@pytest.mark.parametrize("fail_on", ["safe_from", "deferred_since", "watermark"])
def test_a_failed_write_after_a_capped_fire_keeps_the_re_read_range(fail_on):
    """No persistence boundary loses the horizon: the fire stays due, and its retry recovers."""
    coord = _Coordinator(400, deferred_since=700)
    ch = _Ch(now=1000, delay=190)
    worker = _worker(ch, coord=coord, overlap_seconds=120, max_deferral_seconds=300)
    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    coord.fail_on = fail_on

    with pytest.raises(DatabaseError):
        worker.run(RULES_HUNT, 1000)
    assert coord.watermark == 400

    coord.fail_on = ""
    # The replica is still stuck at 800, so a retry whose hold restarted waits out the cap again.
    while worker.hold_for_safe_horizon(RULES_HUNT, 1000):
        ch.now += 60
        ch.delay = ch.now - 810
    worker.run(RULES_HUNT, 1000)
    assert coord.watermark == 1000
    assert coord.marks["safe_from"] == 800

    ch.commands.clear()
    ch.now, ch.delay = 1600, 0
    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1600)
    worker.run(RULES_HUNT, 1600)
    [sql] = ch.commands
    # 820 (fire - 180) is inside the read, and the dedupe skips what an attempt already wrote.
    assert "_timestamp_load >= toDateTime64(680, 3)" in sql
    assert "_uuid NOT IN (SELECT matched_uuid FROM `d`.`det`" in sql


@pytest.mark.parametrize(("target_delay", "counted"), [(0, 0), (5, 1)])
def test_a_capped_fire_on_a_lagging_detection_table_is_counted(manager, target_delay, counted):
    coord = _Coordinator(None, deferred_since=1000)
    ch = _Ch(now=1300, delay=900, target_delay=target_delay)
    worker = _worker(ch, manager=manager, coord=coord, max_deferral_seconds=300)

    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    observed = manager.observed("hunt_safe_horizon_cap_ran_with_target_lag_total")
    assert len(observed) == counted
