#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_safe_horizon.py
#  Purpose:      A row a lagging replica holds back is detected and notified once
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Replication lag on three real ClickHouse replicas, made deterministic.

``SYSTEM STOP FETCHES`` on the replica the runner reads from keeps rows inserted on
another replica out of its reach while their ``_timestamp_load`` sits inside the
fire's window, which is the S3-backed lag a scale cluster shows. Each test then
lets the replica catch up and asserts every matching row became exactly one
detection and was counted in exactly one notification.

The overlap and its dedupe are tested on their own, with the safe horizon off, and
with an overlap of 0 to show the row the horizon-less, overlap-less runner skipped.
"""

import time
import uuid
from types import SimpleNamespace

import clickhouse_connect
import pytest
from common.hunt_files import write_hunt, write_rule
from dfe_schemas.clickhouse import PlatformProbe

from dfe_engine.hunts.ch_coordinator import _COORDINATION_IDS, ChCoordinator
from dfe_engine.hunts.delivery import MatchNotifier
from dfe_engine.hunts.runner import HuntRunner
from dfe_engine.hunts.runner_metrics import HuntRunnerMetrics
from dfe_engine.hunts.schedule import latest_fire
from dfe_engine.hunts.spec_loader import load_specs
from dfe_engine.hunts.worker import HuntWorker
from dfe_engine.schema.applier import SchemaApplier
from dfe_engine.schema.ddl_writer import DDLFileWriter
from dfe_engine.schema.plan import render_one
from tests.support.clickhouse_cleanup import drop_database_and_policies
from tests.support.recording_metrics import RecordingManager

_INTERVAL = 60  # write_hunt's "* * * * *"


class _Notifications:
    """Stands in for Apprise: records the result count of every notification sent."""

    def __init__(self) -> None:
        self.counts: list[int] = []

    def __call__(self, _config):
        return SimpleNamespace(evaluate_and_send=self._send)

    def _send(self, **kwargs) -> bool:
        self.counts.append(int(kwargs["result_count"]))
        return True


@pytest.fixture
def cluster(three_replicas):
    """The real landing, detection and coordination tables in one Replicated database.

    Yields ``(db, writer, reader)``: rows land through replica 1, the hunt runs on
    replica 3, whose fetches a test stops.
    """
    writer, _, reader = (clickhouse_connect.get_client(**p) for p in three_replicas)
    db = f"dfe_lag_{uuid.uuid4().hex[:8]}"
    platform = PlatformProbe(writer).platform
    applier = SchemaApplier(writer, platform)
    applier.ensure_database(db)
    ddl = DDLFileWriter(platform=platform, database=db)
    for spec in (ddl.default_table_spec(), ddl.detection_table_spec()):
        applier.ensure_table(db, spec.name, spec.columns, spec.config)
    for object_id in _COORDINATION_IDS:
        for statement in render_one(object_id, data_database=db, platform=platform).statements:
            writer.command(statement)
    reader.command(f"SYSTEM SYNC DATABASE REPLICA `{db}`")
    try:
        yield db, writer, reader
    finally:
        reader.command(f"SYSTEM START FETCHES `{db}`.`main`")
        drop_database_and_policies(writer, db)


def _hunt(tmp_path, db: str) -> tuple[str, str, dict]:
    org = f"org-{uuid.uuid4().hex[:8]}"
    hunt = f"lag_{uuid.uuid4().hex[:8]}"
    write_rule(tmp_path / "rules", f"{hunt}_rule", f"_org_id = '{org}'")
    write_hunt(tmp_path / "hunts", hunt, f"{hunt}_rule", db, customers=[org])
    specs = load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules")
    return hunt, org, specs


def _land(client, db: str, org: str, count: int, *, newest: int | None = None) -> None:
    """Land *count* rows for *org*, loaded now, or over the 20 seconds before *newest*."""
    loaded = "now64(3)" if newest is None else f"toDateTime64({newest} - 1 - number % 20, 3)"
    client.command(
        f"INSERT INTO `{db}`.`main` (_timestamp_load, _timestamp, _org_id, _json) "
        f"SELECT {loaded}, now64(3), '{org}', '{{}}' FROM numbers({count})"
    )


def _catch_up(reader, db: str) -> None:
    reader.command(f"SYSTEM START FETCHES `{db}`.`main`")
    reader.command(f"SYSTEM SYNC REPLICA `{db}`.`main`")


def _detected(reader, db: str, hunt: str) -> tuple[int, int, int]:
    """(detections, distinct matched rows, matched rows seen more than once) for the hunt."""
    row = reader.query(
        "SELECT count(), uniqExact(matched_uuid), "
        "(SELECT count() FROM (SELECT matched_uuid FROM "
        f"`{db}`.detection WHERE hunt_name = {{h:String}} GROUP BY matched_uuid HAVING count() > 1)) "
        f"FROM `{db}`.detection WHERE hunt_name = {{h:String}}",
        parameters={"h": hunt},
    ).result_rows[0]
    return int(row[0]), int(row[1]), int(row[2])


def _worker(reader, coord, notifications, **kwargs) -> HuntWorker:
    notifier = MatchNotifier(global_channels=["json://localhost"], dispatcher_factory=notifications)
    return HuntWorker(reader, coord, notifier=notifier, **kwargs)


@pytest.mark.parametrize(
    ("overlap", "detected"),
    [
        pytest.param(300, 40, id="the_overlap_detects_every_late_row_once"),
        pytest.param(0, 0, id="without_the_overlap_the_late_rows_are_skipped"),
    ],
)
def test_the_overlap_alone_detects_every_late_row_exactly_once(
    cluster, tmp_path, overlap, detected
):
    db, writer, reader = cluster
    hunt, org, specs = _hunt(tmp_path, db)
    spec = specs[hunt]
    coord = ChCoordinator(reader, database=db, settle_seconds=0.0, sleep=lambda _s: None)
    notifications = _Notifications()
    worker = _worker(reader, coord, notifications, overlap_seconds=overlap, safe_horizon=False)
    fire = int(time.time()) // _INTERVAL * _INTERVAL

    reader.command(f"SYSTEM STOP FETCHES `{db}`.`main`")
    _land(writer, db, org, 40, newest=fire)
    worker.run(spec, fire)
    assert _detected(reader, db, hunt) == (0, 0, 0)

    _catch_up(reader, db)
    worker.run(spec, fire + _INTERVAL)
    worker.run(spec, fire + 2 * _INTERVAL)

    assert _detected(reader, db, hunt) == (detected, detected, 0)
    assert notifications.counts == ([detected] if detected else [])


def test_a_fire_waits_for_the_safe_horizon_then_the_cap_and_the_overlap_finish_the_job(
    cluster, tmp_path
):
    db, writer, reader = cluster
    hunt, org, specs = _hunt(tmp_path, db)
    coord = ChCoordinator(reader, database=db, settle_seconds=0.0, sleep=lambda _s: None)
    manager = RecordingManager()
    notifications = _Notifications()
    worker = _worker(
        reader,
        coord,
        notifications,
        overlap_seconds=300,
        max_deferral_seconds=15,
        metrics=HuntRunnerMetrics(manager),
    )
    runner = HuntRunner(coord, worker, specs, cap=8)

    first = latest_fire(hunt, _INTERVAL, int(time.time())) + _INTERVAL
    _sleep_until(first - 5)
    reader.command(f"SYSTEM STOP FETCHES `{db}`.`main`")
    _land(writer, db, org, 30)
    _sleep_until(first + 1)
    # The replica is behind and the fire is younger than the cap, so it waits unclaimed.
    assert runner.tick(int(time.time())) == 0
    assert coord.get_watermark(hunt) is None
    _catch_up(reader, db)
    assert runner.tick(int(time.time())) == 1
    assert _detected(reader, db, hunt) == (30, 30, 0)

    second = first + _INTERVAL
    _sleep_until(second - 5)
    reader.command(f"SYSTEM STOP FETCHES `{db}`.`main`")
    _land(writer, db, org, 20)
    _sleep_until(second + 1)
    assert runner.tick(int(time.time())) == 0
    # Past the cap the fire runs short of the horizon, missing what the replica lacks.
    _sleep_until(second + 16)
    assert runner.tick(int(time.time())) == 1
    assert coord.get_watermark(hunt) == second
    assert _detected(reader, db, hunt) == (30, 30, 0)
    assert len(manager.observed("hunt_safe_horizon_cap_reached_total")) == 1

    _catch_up(reader, db)
    _sleep_until(second + _INTERVAL + 1)
    assert runner.tick(int(time.time())) == 1

    assert _detected(reader, db, hunt) == (50, 50, 0)
    assert notifications.counts == [30, 20]
    assert len(manager.observed("hunt_safe_horizon_deferrals_total")) == 2
    skipped = [o.value for o in manager.observed("hunt_duplicates_skipped_total")]
    assert skipped == [0, 30, 30]


def _sleep_until(epoch: int) -> None:
    """Wait for the real clock, which both the runner and the server's horizon read."""
    remaining = epoch - time.time()
    if remaining > 0:
        time.sleep(remaining)


def test_rows_a_capped_fire_could_not_read_are_detected_once_by_the_next_fire(cluster, tmp_path):
    """The cap lets a fire run short of the horizon, and the next fire re-reads from there.

    The rows are loaded about 40 s before the fire, further back than the 10 s overlap
    reaches, so only the re-read from the recorded horizon can find them.
    """
    db, writer, reader = cluster
    hunt, org, specs = _hunt(tmp_path, db)
    spec = specs[hunt]
    coord = ChCoordinator(reader, database=db, settle_seconds=0.0, sleep=lambda _s: None)
    manager = RecordingManager()
    worker = _worker(
        reader,
        coord,
        _Notifications(),
        overlap_seconds=10,
        max_deferral_seconds=300,
        metrics=HuntRunnerMetrics(manager),
    )

    reader.command(f"SYSTEM STOP FETCHES `{db}`.`main`")
    _land(writer, db, org, 20)
    time.sleep(40)
    fire = int(time.time())
    coord.set_watermark(hunt, fire - _INTERVAL)
    # Held since well past the cap, as a hunt held across earlier fires would be.
    coord.set_safe_horizon_mark(hunt, "deferred_since", fire - 1000)
    assert not worker.hold_for_safe_horizon(spec, fire)
    worker.run(spec, fire)
    safe_from = coord.get_safe_horizon_mark(hunt, "safe_from")
    assert safe_from is not None
    assert safe_from < fire - 30
    assert coord.get_watermark(hunt) == fire
    assert _detected(reader, db, hunt) == (0, 0, 0)

    _catch_up(reader, db)
    assert not worker.hold_for_safe_horizon(spec, fire + _INTERVAL)
    worker.run(spec, fire + _INTERVAL)
    worker.run(spec, fire + 2 * _INTERVAL)

    assert _detected(reader, db, hunt) == (20, 20, 0)
    assert coord.get_safe_horizon_mark(hunt, "safe_from") is None
    assert coord.get_safe_horizon_mark(hunt, "deferred_since") is None
    assert len(manager.observed("hunt_safe_horizon_cap_reached_total")) == 1
