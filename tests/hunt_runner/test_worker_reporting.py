#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_worker_reporting.py
#  Purpose:      The one line and the one counter a completed fire leaves behind
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A fire has to say what it did, or a hunt failure is undiagnosable (#357).

On the rc.13 docker stack the runner logged nothing after startup: three fires ran,
rows landed in the detection table, and the container's last line was still the one
it printed while resolving its engine. So these assert the LINE - that it carries
the hunt, the rows and the duration - and the rows counter beside it.
"""

from __future__ import annotations

from scalo.logger import logger

from dfe_engine.hunt_runner import HuntSpec, HuntWorker
from dfe_engine.hunt_runner.metrics import HuntRunnerMetrics

from .conftest import Observation

SPEC = HuntSpec(
    hunt_id="brute-force",
    interval_seconds=60,
    queries=["INSERT INTO dfe.detection SELECT * FROM dfe.events WHERE {window}"],
)


class _FakeSummary:
    def __init__(self, written_rows: int) -> None:
        self.written_rows = written_rows


class _FakeCh:
    """A ClickHouse client that reports what its INSERT wrote."""

    def __init__(self, written_rows: int) -> None:
        self._written_rows = written_rows
        self.commands: list[str] = []

    def command(self, sql: str, settings: dict | None = None) -> _FakeSummary:
        self.commands.append(sql)
        return _FakeSummary(self._written_rows)


class _FakeCoordinator:
    def __init__(self) -> None:
        self.runs: list[tuple[str, int, int]] = []

    def get_watermark(self, hunt_id: str) -> int | None:
        return None

    def set_watermark(self, hunt_id: str, watermark: int) -> None:
        pass

    def record_run(self, hunt_id: str, fire: int, rows_written: int) -> None:
        self.runs.append((hunt_id, fire, rows_written))


def _run_capturing_logs(worker: HuntWorker) -> list:
    captured: list = []
    handler_id = logger.add(captured.append, level="INFO")
    try:
        worker.run(SPEC, scheduled_start=600)
    finally:
        logger.remove(handler_id)
    return [m for m in captured if "hunt fire complete" in m]


def test_a_completed_fire_logs_one_line_with_the_hunt_rows_and_duration():
    worker = HuntWorker(_FakeCh(written_rows=7), _FakeCoordinator())

    lines = _run_capturing_logs(worker)

    assert len(lines) == 1
    extra = lines[0].record["extra"]
    assert extra["hunt_id"] == "brute-force"
    assert extra["fire"] == 600
    assert extra["rows_written"] == 7
    assert extra["duration_seconds"] >= 0.0


def test_a_fire_that_found_nothing_still_says_so():
    """Zero rows is a result. Silence is what makes a stopped hunt invisible."""
    worker = HuntWorker(_FakeCh(written_rows=0), _FakeCoordinator())

    lines = _run_capturing_logs(worker)

    assert len(lines) == 1
    assert lines[0].record["extra"]["rows_written"] == 0


def test_the_rows_a_fire_wrote_reach_the_counter(manager):
    worker = HuntWorker(
        _FakeCh(written_rows=7), _FakeCoordinator(), metrics=HuntRunnerMetrics(manager)
    )

    worker.run(SPEC, scheduled_start=600)

    assert manager.observed("hunt_rows_written_total") == [
        Observation("hunt_rows_written_total", {"hunt_id": "brute-force"}, "inc", 7)
    ]


def test_a_worker_with_no_metrics_backend_still_runs_the_hunt():
    coord = _FakeCoordinator()
    worker = HuntWorker(_FakeCh(written_rows=3), coord)

    worker.run(SPEC, scheduled_start=600)

    assert coord.runs == [("brute-force", 600, 3)]
