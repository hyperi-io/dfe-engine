#  Project:      dfe-engine
#  File:         tests/integration/test_clickhouse_cleanup.py
#  Purpose:      A dropped test database takes its row policies with it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``DROP DATABASE`` leaves row policies behind on a real server, so the helper drops them."""

import uuid

import pytest

from tests.support.clickhouse_cleanup import drop_database_and_policies

pytestmark = pytest.mark.integration


def _policies(ch_client, database: str) -> int:
    return int(
        ch_client.query(
            "SELECT count() FROM system.row_policies WHERE database = {db:String}",
            parameters={"db": database},
        ).result_rows[0][0]
    )


def _databases(ch_client, database: str) -> int:
    return int(
        ch_client.query(
            "SELECT count() FROM system.databases WHERE name = {db:String}",
            parameters={"db": database},
        ).result_rows[0][0]
    )


@pytest.fixture
def database(ch_client):
    """A database holding one table with a row policy, and a second the helper must not touch."""
    name = f"dfe_test_cleanup_{uuid.uuid4().hex[:8]}"
    neighbour = f"{name}_neighbour"
    for db in (name, neighbour):
        ch_client.command(f"CREATE DATABASE `{db}`")
        ch_client.command(f"CREATE TABLE `{db}`.`t` (a String) ENGINE = MergeTree ORDER BY a")
        ch_client.command(f"CREATE ROW POLICY `p` ON `{db}`.`t` USING 1 TO ALL")
    yield name, neighbour
    drop_database_and_policies(ch_client, name)
    drop_database_and_policies(ch_client, neighbour)


def test_a_plain_drop_database_leaves_the_policy_behind(ch_client, database):
    name, _neighbour = database

    ch_client.command(f"DROP DATABASE `{name}` SYNC")

    assert (_databases(ch_client, name), _policies(ch_client, name)) == (0, 1)


def test_the_helper_removes_the_database_and_its_policies_only(ch_client, database):
    name, neighbour = database

    drop_database_and_policies(ch_client, name)

    assert (_databases(ch_client, name), _policies(ch_client, name)) == (0, 0)
    assert (_databases(ch_client, neighbour), _policies(ch_client, neighbour)) == (1, 1)


def test_dropping_a_database_that_is_already_gone_is_not_an_error(ch_client, database):
    name, _neighbour = database
    drop_database_and_policies(ch_client, name)

    drop_database_and_policies(ch_client, name)
