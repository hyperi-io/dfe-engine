#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_autodisable.py
#  Purpose:      A hunt that keeps busting a ClickHouse limit is stopped, and only the API restarts it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hunt disable and auto-disable on real ClickHouse, no mocks.

The hunt's own SQL trips ``max_rows_to_read`` (code 158), a resource limit, so
the runner stops it after the configured failures and writes why into hunt_state.
The API's clear is the one way back. A hunt whose YAML says ``enabled: false`` is
dropped from the specs and tombstoned in ``hunt_schedule``, so KEDA stops counting it.
"""

from pathlib import Path

import pytest

from dfe_engine.hunts.ch_coordinator import ChCoordinator
from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.hunts.run_status import clear_auto_disable, read_run_status
from dfe_engine.hunts.runner import HuntFailures, HuntRunner
from dfe_engine.hunts.schedule import current_fire, publish_schedule, read_schedule
from dfe_engine.hunts.spec_loader import load_specs
from dfe_engine.hunts.worker import HuntWorker

LIMIT_SQL = (
    "SELECT count() FROM (SELECT toDateTime64(number, 3) AS _timestamp_load FROM numbers(1000)) "
    "WHERE {window} SETTINGS max_rows_to_read = 10"
)


class _CountingClient:
    """The real client, counting the fires of the hunt's own SQL among every statement."""

    def __init__(self, ch) -> None:
        self._ch = ch
        self.fires = 0

    def command(self, sql: str, *args, **kwargs):
        if "max_rows_to_read" in sql:
            self.fires += 1
        return self._ch.command(sql, *args, **kwargs)

    def query(self, *args, **kwargs):
        return self._ch.query(*args, **kwargs)

    def insert(self, *args, **kwargs):
        return self._ch.insert(*args, **kwargs)


@pytest.fixture
def state_db(ch_client, dfe_db):
    """The coordination database once hunt_state carries the auto-disable columns."""
    columns = {
        r[0]
        for r in ch_client.query(
            "SELECT name FROM system.columns WHERE database = {d:String} AND table = 'hunt_state'",
            parameters={"d": dfe_db},
        ).result_rows
    }
    if "auto_disabled_at" not in columns:
        pytest.skip("hunt_state 1.1.0 (auto_disabled_at) is not in the pinned dfe-schemas release")
    return dfe_db


def test_k_limit_failures_stop_the_hunt_until_the_api_clears_it(ch_client, state_db):
    ch = _CountingClient(ch_client)
    coord = ChCoordinator(ch, database=state_db, sleep=lambda _s: None, monotonic=lambda: 0.0)
    coord.ensure_schema()
    spec = HuntSpec(hunt_id="h", interval_seconds=600, queries=[LIMIT_SQL])
    failures = HuntFailures(
        initial_seconds=0, max_seconds=0, jitter=lambda w: 0.0, autodisable_failures=2
    )
    runner = HuntRunner(coord, HuntWorker(ch, coord), {"h": spec}, cap=4, failures=failures)
    now = current_fire("h", 600, 5000) + 1
    # The fixture SQL trips the row limit, a resource limit, and nothing else.
    with pytest.raises(Exception, match="Code: 158"):
        HuntWorker(ch, coord).run(spec, scheduled_start=now)
    assert ch.fires == 1

    runner.tick(now)
    assert ch.fires == 2
    assert coord.auto_disabled_hunts() == {}
    runner.tick(now + 1)
    assert ch.fires == 3, (coord.get_state("h"), coord.current_lease("h"))
    assert coord.auto_disabled_hunts() == {"h": "resource_limit:158"}
    status = read_run_status(ch_client, state_db, ["h"], now + 1)["h"]
    assert (status.auto_disabled_at, status.auto_disabled_reason) == (now + 1, "resource_limit:158")
    # Stopped: a tick with the back-off at zero fires nothing.
    runner.tick(now + 2)
    assert ch.fires == 3

    clear_auto_disable(ch_client, state_db, "h")
    assert coord.auto_disabled_hunts() == {}
    assert read_run_status(ch_client, state_db, ["h"], now + 3)["h"].auto_disabled_at is None
    runner.tick(now + 3)
    assert ch.fires == 4

    # Stopped and cleared a second time: the clear must not be dropped as a duplicate
    # of the first clear's insert block by the replicated table.
    coord.record_auto_disable("h", "resource_limit:158", now + 4)
    assert coord.auto_disabled_hunts() == {"h": "resource_limit:158"}
    clear_auto_disable(ch_client, state_db, "h")
    assert coord.auto_disabled_hunts() == {}
    assert read_run_status(ch_client, state_db, ["h"], now + 5)["h"].auto_disabled_at is None


def test_a_hunt_disabled_in_yaml_is_tombstoned_in_the_schedule(ch_client, dfe_db, tmp_path: Path):
    hunt_dir = tmp_path / "hunts"
    hunt_dir.mkdir()
    (hunt_dir / "keep.yaml").write_text('cron: "*/10 * * * *"\nquery: "SELECT 1 WHERE {window}"\n')
    (hunt_dir / "stop.yaml").write_text('cron: "*/10 * * * *"\nquery: "SELECT 1 WHERE {window}"\n')
    publish_schedule(ch_client, dfe_db, load_specs(hunt_dir))
    assert set(read_schedule(ch_client, dfe_db)) == {"keep", "stop"}

    (hunt_dir / "stop.yaml").write_text(
        'cron: "*/10 * * * *"\nquery: "SELECT 1 WHERE {window}"\nenabled: false\n'
    )
    publish_schedule(ch_client, dfe_db, load_specs(hunt_dir))
    assert set(read_schedule(ch_client, dfe_db)) == {"keep"}

    (hunt_dir / "stop.yaml").write_text('cron: "*/10 * * * *"\nquery: "SELECT 1 WHERE {window}"\n')
    publish_schedule(ch_client, dfe_db, load_specs(hunt_dir))
    assert set(read_schedule(ch_client, dfe_db)) == {"keep", "stop"}
