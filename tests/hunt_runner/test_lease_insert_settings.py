#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_lease_insert_settings.py
#  Purpose:      Every lease write keeps identical rows, the claim's INSERT ... SELECT included
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the coordinator hands the driver for each lease write.

ClickHouse 26.3 never deduplicates an INSERT ... SELECT, so a real server cannot show
the claim needing its settings. 24.8 does drop an identical one, so the settings are
asserted on what the driver is handed. The real-server sequences are in
``tests/integration/test_ch_coordinator.py``.
"""

from types import SimpleNamespace
from typing import Any

from dfe_engine.hunt_runner.ch_coordinator import ChCoordinator

KEEP = {"insert_deduplicate": 0, "deduplicate_insert": "disable"}


class _RecordingDriver:
    """A clickhouse-connect-shaped client that records writes and answers the reads."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.settings_reads = 0

    def query(self, sql: str, parameters=None, **_kwargs) -> SimpleNamespace:
        if "system.settings" in sql:
            self.settings_reads += 1
            return SimpleNamespace(result_rows=[(1,)])
        # The newest lease row is this worker's live claim.
        return SimpleNamespace(result_rows=[("w1", 1000, 10_000)])

    def command(self, sql: str, **kwargs) -> None:
        self.writes.append((sql, kwargs))

    def insert(self, table: str, data, **kwargs) -> None:
        self.writes.append((table, kwargs))


def _coordinator(driver: _RecordingDriver) -> ChCoordinator:
    return ChCoordinator(
        driver, database="dfe", worker_id="w1", settle_seconds=0.0, sleep=lambda _s: None
    )


def test_the_claim_insert_select_keeps_identical_rows():
    driver = _RecordingDriver()

    assert _coordinator(driver).try_claim("h", fire=1000, now=100) is True

    (claim,) = [kwargs for sql, kwargs in driver.writes if sql.startswith("INSERT INTO")]
    assert claim["settings"] == KEEP


def test_every_lease_write_keeps_identical_rows_and_asks_the_server_once():
    driver = _RecordingDriver()
    coord = _coordinator(driver)

    coord.try_claim("h", fire=1000, now=100)
    coord.renew("h", fire=1000, now=150)
    coord.release("h", fire=1000)
    coord.try_claim("h", fire=1000, now=200)
    coord.release("h", fire=1000)

    assert len(driver.writes) == 5
    assert all(kwargs["settings"] == KEEP for _target, kwargs in driver.writes)
    assert driver.settings_reads == 1
