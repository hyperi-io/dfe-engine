#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_table_exists_ch.py
#  Purpose:      A table-exists read answers from the server, and a server it cannot reach is an error
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``clickhouse_table_exists`` on a real ClickHouse and on an address nothing listens on.

A read that fails must not come back as "no such table": the redeploy planner reads
that as a table to create, and skips the TTL and settings reconcile it owes an
existing one.
"""

import pytest
from fastapi import HTTPException

from dfe_engine.api.v1.schemas import _dedicated_source_table_ready
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema.applier import SchemaApplyError
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.services.schema.json_promotion_service import clickhouse_table_exists
from dfe_engine.source.deployment import plan_deploy_statements
from dfe_engine.source.models import Source

pytestmark = pytest.mark.integration


@pytest.fixture
def database(clickhouse_test_database):
    """A throwaway Replicated database, dropped afterwards."""
    return clickhouse_test_database


@pytest.fixture
def unreachable(late_clickhouse):
    """The engine's client wrapper on an address with no server, giving up in a second."""
    params, _start = late_clickhouse
    manager = ClickHouseManager(
        target_config_data={
            "ch_host": params["host"],
            "ch_port": params["port"],
            "ch_username": params["username"],
            "ch_password": params["password"],
            "ch_secure": params["secure"],
        }
    )
    try:
        yield manager.get_bounded_client(1.0)
    finally:
        manager.close()


def _source(name: str) -> Source:
    return Source.model_validate(
        {
            "source": name,
            "match": {"field": "tags.collector.type", "value": name},
            "header": {"type": "timeseries", "version": "1.0.0"},
            "schema": {"ttl_days": 90, "engine": ""},
        }
    )


def test_a_table_is_found_through_the_raw_client_and_the_wrapper(
    ch_client, manager_client, database
):
    ch_client.command(
        f"CREATE TABLE `{database}`.`present` (x UInt8) ENGINE = MergeTree ORDER BY x"
    )

    for client in (ch_client, manager_client):
        assert clickhouse_table_exists(client, database, "present") is True
        assert clickhouse_table_exists(client, database, "absent") is False
        assert clickhouse_table_exists(client, f"{database}_none", "present") is False


def test_an_unreachable_clickhouse_is_an_error_and_not_an_absent_table(unreachable):
    with pytest.raises(SchemaApplyError, match="could not read ClickHouse state"):
        clickhouse_table_exists(unreachable, "dfe", "anything")


def test_a_redeploy_plan_against_an_unreachable_clickhouse_raises(unreachable):
    source = _source("unreachable-probe")
    builder = SchemaBuilderV2()
    result = builder.build(source)
    assert not result.validation_errors, result.validation_errors

    with pytest.raises(SchemaApplyError):
        plan_deploy_statements(
            builder, source, source.runtime_version_id(), result, db="dfe", ch_client=unreachable
        )


def test_discovery_refuses_with_422_when_clickhouse_cannot_be_read(unreachable):
    source = _source("discovery-probe")
    deployed = source.model_copy(update={"deployed_version": source.current})

    with pytest.raises(HTTPException) as refused:
        _dedicated_source_table_ready(
            deployed, deployed.current, ch=unreachable, db="dfe", table=deployed.table_name
        )

    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "discovery_failed"  # ty: ignore[invalid-argument-type]  # starlette types detail as str, and the engine raises it with a dict
