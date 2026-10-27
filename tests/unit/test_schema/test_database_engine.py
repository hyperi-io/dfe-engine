#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_database_engine.py
#  Purpose:      A DFE database that predates Replicated is refused with one clear reason
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The plan refuses an existing database without the Replicated engine, before any DDL runs.

CREATE DATABASE IF NOT EXISTS leaves an Atomic database as it is, and the first table
that names the ``{uuid}`` macro then fails with ClickHouse code 36. The plan reads
the engine first so the status route carries a sentence an operator can act on.
"""

from types import SimpleNamespace

import pytest
from dfe_schemas.clickhouse import Platform

from dfe_engine.schema.phase import _failed_stage
from dfe_engine.schema.plan import DatabaseEngineError, build_plan
from dfe_engine.settings import DFESettings


class _Server:
    """A self-hosted server that has Keeper and answers for the databases it holds."""

    def __init__(self, databases: dict[str, str]) -> None:
        self.databases = databases
        self.statements: list[str] = []

    def query(self, sql: str, parameters: dict[str, str] | None = None):
        self.statements.append(sql)
        name = (parameters or {}).get("name", "")
        if "system.databases" in sql:
            rows = [[self.databases[name]]] if name in self.databases else []
        elif "system.macros" in sql:
            rows = [["cluster", "dfe"], ["shard", "1"], ["replica", "r1"]]
        elif "system.clusters" in sql or "system.tables" in sql:
            rows = [[1]]
        else:
            rows = [[0]]
        return SimpleNamespace(result_rows=rows)


def _settings() -> DFESettings:
    return DFESettings(env="dev")


@pytest.mark.parametrize(
    ("held", "refused"),
    [
        pytest.param({}, None, id="fresh_server"),
        pytest.param({"dfe": "Replicated", "dfe_meta": "Replicated"}, None, id="replicated"),
        pytest.param({"dfe": "Shared", "dfe_meta": "Shared"}, None, id="shared"),
        pytest.param({"dfe": "Atomic"}, "dfe", id="atomic_data_database"),
        pytest.param({"dfe": "Replicated", "dfe_meta": "Atomic"}, "dfe_meta", id="atomic_meta"),
        pytest.param({"dfe": "Ordinary"}, "dfe", id="ordinary"),
    ],
)
def test_an_existing_database_must_be_replicated(held, refused):
    server = _Server(held)

    if refused is None:
        build_plan(settings=_settings(), client=server, platform=Platform.SELF_HOSTED)
        return
    with pytest.raises(DatabaseEngineError) as raised:
        build_plan(settings=_settings(), client=server, platform=Platform.SELF_HOSTED)

    assert f"database {refused!r} uses the {held[refused]} engine" in str(raised.value)
    assert "fresh deploy" in str(raised.value)


def test_a_cloud_platform_is_not_asked():
    server = _Server({"dfe": "Atomic"})

    build_plan(settings=_settings(), client=server, platform=Platform.CLOUD)

    assert not any("system.databases" in sql for sql in server.statements)


def test_the_status_route_carries_the_reason_and_not_the_stage():
    with pytest.raises(DatabaseEngineError) as raised:
        build_plan(
            settings=_settings(),
            client=_Server({"dfe": "Atomic"}),
            platform=Platform.SELF_HOSTED,
        )

    assert _failed_stage(raised.value) == str(raised.value)
