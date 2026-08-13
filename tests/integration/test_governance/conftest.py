#  Project:      dfe-engine
#  File:         tests/integration/test_governance/conftest.py
#  Purpose:      Shared live-ClickHouse fixtures for the governance integration tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Connection fixtures shared by the CH-RBAC integration modules."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def conn_params():
    """Module-scoped CH connection params from settings (.env cluster tier)."""
    from dfe_engine.settings import get_settings

    s = get_settings().clickhouse
    if not s.host or s.host in ("localhost", "127.0.0.1"):
        pytest.skip("CH-RBAC isolation needs a configured cluster (.env DFE_CLICKHOUSE_*)")
    return {
        "host": s.host,
        "port": s.port,
        "username": s.username,
        "password": s.password,
        "secure": s.secure,
    }


@pytest.fixture(scope="module")
def admin_client(conn_params):
    """A module-scoped admin CH client (raw clickhouse_connect, has command())."""
    import clickhouse_connect

    client = clickhouse_connect.get_client(**conn_params)
    yield client
    client.close()


def count_as(params: dict, user: str, password: str, table_fqn: str) -> int:
    """Connect to CH as ``user`` and return ``count()`` of the visible rows."""
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=params["host"],
        port=params["port"],
        username=user,
        password=password,
        secure=params["secure"],
    )
    try:
        return int(client.query(f"SELECT count() FROM {table_fqn}").result_rows[0][0])
    finally:
        client.close()


def drop_safely(ch_client, stmt: str) -> None:
    """Run a DROP, ignoring failure so teardown keeps dropping the rest."""
    try:
        ch_client.command(stmt)
    except Exception:
        pass
