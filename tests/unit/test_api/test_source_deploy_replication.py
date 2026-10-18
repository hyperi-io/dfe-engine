#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_source_deploy_replication.py
#  Purpose:      A source deploy refuses the server the schema phase refuses, with the same reason
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The deploy route's replication check, against a stand-in for the probe's server.

The stand-in answers the probe's own queries the way a server does. The real
servers are exercised in ``tests/integration/test_schema/test_platform_ddl_ch.py``.
"""

from types import SimpleNamespace

import pytest
from dfe_schemas.clickhouse import PlatformProbe
from fastapi import HTTPException

from dfe_engine.api.v1 import sources as sources_module


class _Server:
    """Answers the probe's queries for a self-hosted server, with or without Keeper."""

    def __init__(self, *, keeper: bool) -> None:
        self._keeper = keeper

    def query(self, sql: str, parameters=None) -> SimpleNamespace:
        if "system.table_engines" in sql:
            return SimpleNamespace(result_rows=[(0,)])
        if "system.tables" in sql:
            return SimpleNamespace(result_rows=[(1 if self._keeper else 0,)])
        if "system.macros" in sql:
            rows = [("cluster", "dfe"), ("shard", "01"), ("replica", "r1")]
            return SimpleNamespace(result_rows=rows)
        if "system.clusters" in sql:
            return SimpleNamespace(result_rows=[(1,)])
        raise AssertionError(f"unexpected query: {sql}")


class _Unreachable:
    def query(self, sql: str, parameters=None) -> SimpleNamespace:
        raise ConnectionError("Code: 210. DB::NetException: Connection refused (ch.internal:8123)")


def test_a_server_with_keeper_and_the_macros_passes():
    sources_module._require_replication(PlatformProbe(_Server(keeper=True)), "acme")


def test_a_keeperless_server_answers_503_with_the_schema_phases_reason():
    with pytest.raises(HTTPException) as raised:
        sources_module._require_replication(PlatformProbe(_Server(keeper=False)), "acme")

    assert raised.value.status_code == 503
    detail = raised.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "clickhouse_not_replicated"
    assert "is missing Keeper" in detail["message"]
    assert "docs/clickhouse-platform.md" in detail["message"]


def test_a_server_that_cannot_be_asked_answers_503_without_its_text(audit_events):
    with pytest.raises(HTTPException) as raised:
        sources_module._require_replication(PlatformProbe(_Unreachable()), "acme")

    assert raised.value.status_code == 503
    detail = raised.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "clickhouse_unavailable"
    assert "ch.internal" not in str(detail)
