#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_worker_heartbeat.py
#  Purpose:      The worker heartbeats its lease during a long query (P2.6)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A run longer than lease_seconds must renew the lease mid-flight, or a second
pod reclaims the SAME fire and double-runs it. The heartbeat renews at a third of
the TTL until the query returns. No live ClickHouse: the query is a blocking stub
and the coordinator is a spy that records renews."""

from __future__ import annotations

import time

from dfe_engine.hunt_runner.models import HuntSpec
from dfe_engine.hunt_runner.worker import HuntWorker


class _SlowCh:
    def command(self, sql, settings=None) -> None:
        time.sleep(1.5)  # longer than the 1s heartbeat interval (lease 3 / 3)


class _SpyCoord:
    lease_seconds = 3

    def __init__(self) -> None:
        self.renews: list[tuple[str, int]] = []
        self.watermark_set: int | None = None

    def get_watermark(self, hunt_id):
        return None

    def set_watermark(self, hunt_id, end):
        self.watermark_set = end

    def renew(self, hunt_id, fire):
        self.renews.append((hunt_id, fire))


def test_worker_renews_lease_during_long_query():
    coord = _SpyCoord()
    worker = HuntWorker(_SlowCh(), coord)
    spec = HuntSpec(hunt_id="h1", interval_seconds=60, query="INSERT x WHERE {window}")

    worker.run(spec, scheduled_start=1000)

    # at least one heartbeat fired during the 1.5s query (interval == 1s)
    assert coord.renews, "expected a lease renew during the long query"
    assert all(hid == "h1" and fire == 1000 for hid, fire in coord.renews)
    # watermark still advanced after the query committed
    assert coord.watermark_set is not None
