#  Project:      dfe-engine
#  File:         tests/integration/test_governance/conftest.py
#  Purpose:      Shared live-ClickHouse helpers for the governance integration tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Helpers shared by the CH-RBAC integration modules.

The server comes from the tiered harness in ``tests/integration/conftest.py``
(``ch_params`` / ``ch_client``): a configured cluster, a remote docker host, or a
throwaway local container, each owned by the one test.
"""

from __future__ import annotations


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
