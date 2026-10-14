#  Project:      dfe-engine
#  File:         tests/support/down_clickhouse.py
#  Purpose:      The engine's own ClickHouse client, aimed at a server that drops every connection
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A real bounded ClickHouse client whose server is down.

The client is the one a console read uses, with its own resilience and a short
budget, pointed at a ``CountingListener`` that accepts and drops each connection.
A test sees exactly what a down server gives the code under test, and the
listener's count says whether that code dialled at all.
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from tests.support.loopback import CountingListener

DOWN_BUDGET_SECONDS = 0.2
"""How long the client keeps retrying the dropped connection before it gives up."""


@contextmanager
def down_clickhouse(settings: Any) -> Generator[tuple[CountingListener, Any]]:
    """Yield the listener and a bounded client aimed at it; both closed afterwards.

    Args:
        settings: Engine settings supplying the client's resilience configuration.
    """
    listener = CountingListener()
    manager = ClickHouseManager(
        target_config_data={
            "ch_host": "127.0.0.1",
            "ch_port": listener.port,
            "ch_username": "default",
            "ch_password": "",
            "ch_secure": False,
        },
        settings=settings,
    )
    try:
        yield listener, manager.get_bounded_client(DOWN_BUDGET_SECONDS)
    finally:
        manager.close()
        listener.close()
