#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_statements.py
#  Purpose:      A script splits only where a statement ends, and DDL gets the size it needs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``split_statements`` and ``ddl_settings``, and the wrapper's ``execute`` over them.

The wrapper cases run on a recording stand-in for the clickhouse-connect driver,
the one boundary a unit test cannot cross; every assertion reads what the driver
was actually handed.
"""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.clickhouse.statements import (
    CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE,
    MAX_QUERY_SIZE_MARGIN,
    StatementTooLargeError,
    ddl_settings,
    split_statements,
)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("SELECT 1", ["SELECT 1"]),
        ("  SELECT 1 ;  ", ["SELECT 1"]),
        ("SELECT 1; SELECT 2;", ["SELECT 1", "SELECT 2"]),
        (
            "CREATE ROLE r COMMENT 'a;b'; SELECT 2",
            ["CREATE ROLE r COMMENT 'a;b'", "SELECT 2"],
        ),
        ("SELECT 'it''s; here'; SELECT 2", ["SELECT 'it''s; here'", "SELECT 2"]),
        ("SELECT 'esc\\'; here'; SELECT 2", ["SELECT 'esc\\'; here'", "SELECT 2"]),
        ("SELECT `a;b` FROM t; SELECT 2", ["SELECT `a;b` FROM t", "SELECT 2"]),
        ('SELECT "c;d" FROM t; SELECT 2', ['SELECT "c;d" FROM t', "SELECT 2"]),
        ("SELECT 1 -- note; here\n; SELECT 2", ["SELECT 1 -- note; here", "SELECT 2"]),
        ("SELECT 1 /* note; here */; SELECT 2", ["SELECT 1 /* note; here */", "SELECT 2"]),
        ("SELECT $$a;b$$; SELECT 2", ["SELECT $$a;b$$", "SELECT 2"]),
        ("SELECT {db:String}; SELECT 2", ["SELECT {db:String}", "SELECT 2"]),
    ],
    ids=[
        "one",
        "trailing-semicolon",
        "two",
        "single-quoted",
        "doubled-quote",
        "backslash-escape",
        "backticked",
        "double-quoted",
        "line-comment",
        "block-comment",
        "heredoc",
        "query-parameter",
    ],
)
def test_a_script_splits_only_where_a_statement_ends(script, expected):
    assert split_statements(script) == expected


@pytest.mark.parametrize("script", ["", "   ", ";", " ; ;\n;"])
def test_a_script_with_no_statement_splits_into_nothing(script):
    assert split_statements(script) == []


def test_an_unterminated_quote_is_returned_whole_for_clickhouse_to_refuse():
    assert split_statements("SELECT 'open; SELECT 2;") == ["SELECT 'open; SELECT 2"]


def _statement_of(size: int) -> str:
    """A statement exactly ``size`` bytes long."""
    head = "SELECT '"
    return head + "x" * (size - len(head) - 1) + "'"


def test_a_statement_within_the_default_is_sent_without_the_setting():
    statement = _statement_of(CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE - MAX_QUERY_SIZE_MARGIN)
    assert ddl_settings(statement, ceiling=10**9) == {}


def test_a_statement_one_byte_over_gets_its_own_size_plus_the_margin():
    size = CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE - MAX_QUERY_SIZE_MARGIN + 1
    statement = _statement_of(size)
    assert ddl_settings(statement, ceiling=10**9) == {
        "max_query_size": size + MAX_QUERY_SIZE_MARGIN
    }


def test_the_size_is_the_statements_bytes_not_its_characters():
    """ClickHouse limits bytes, and a comment carrying non-ASCII is longer than its length."""
    statement = "SELECT '" + "é" * 200_000 + "'"
    expected = len(statement.encode("utf-8")) + MAX_QUERY_SIZE_MARGIN
    assert len(statement) + MAX_QUERY_SIZE_MARGIN < CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE
    assert ddl_settings(statement, ceiling=10**9) == {"max_query_size": expected}


def test_the_widest_vendor_table_measured_gets_a_setting_sized_to_it():
    statement = _statement_of(688_321)
    assert ddl_settings(statement, ceiling=10**9) == {
        "max_query_size": 688_321 + MAX_QUERY_SIZE_MARGIN
    }


def test_a_statement_over_the_ceiling_is_refused_before_it_is_sent():
    statement = _statement_of(600_000)
    with pytest.raises(StatementTooLargeError, match="DFE_CLICKHOUSE_DDL_MAX_QUERY_SIZE"):
        ddl_settings(statement, ceiling=500_000)


def test_the_ceiling_defaults_to_the_deployment_setting():
    from dfe_engine.settings import get_settings

    ceiling = get_settings().clickhouse.ddl_max_query_size
    assert ddl_settings(_statement_of(300_000)) == {
        "max_query_size": 300_000 + MAX_QUERY_SIZE_MARGIN
    }
    with pytest.raises(StatementTooLargeError):
        ddl_settings(_statement_of(ceiling))


class _RecordingConnectClient:
    """A clickhouse-connect-shaped client that records what it is sent."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []

    def command(self, statement: str, *args: Any, **kwargs: Any) -> str:
        self.commands.append((statement, kwargs))
        return ""

    def query(self, statement: str, *args: Any, **kwargs: Any) -> SimpleNamespace:
        self.queries.append(statement)
        return SimpleNamespace(result_rows=[(len(self.queries),)])


@pytest.fixture
def wrapper_over_recorder():
    manager = ClickHouseManager()
    recorder = _RecordingConnectClient()
    manager._client = cast("Any", recorder)
    return manager.get_clickhouse_client(), recorder


def test_execute_sends_a_statement_with_a_semicolon_in_a_literal_whole(wrapper_over_recorder):
    client, recorder = wrapper_over_recorder
    statement = "ALTER TABLE t ADD COLUMN c String DEFAULT 'a;b' COMMENT 'x; y'"

    client.execute(statement)

    assert [sent for sent, _ in recorder.commands] == [statement]


def test_execute_runs_each_statement_of_a_script_in_order(wrapper_over_recorder):
    client, recorder = wrapper_over_recorder

    rows = client.execute("CREATE ROLE a COMMENT 'x;y'; SELECT 1; SELECT 2")

    assert [sent for sent, _ in recorder.commands] == ["CREATE ROLE a COMMENT 'x;y'"]
    assert recorder.queries == ["SELECT 1", "SELECT 2"]
    assert rows == [(1,), (2,)]


def test_execute_passes_the_ddl_size_through_to_the_driver(wrapper_over_recorder):
    client, recorder = wrapper_over_recorder
    statement = "ALTER TABLE t COMMENT COLUMN c '" + "x" * 300_000 + "'"

    client.execute(statement, settings=ddl_settings(statement, ceiling=10**9))

    (_sent, kwargs) = recorder.commands[0]
    assert kwargs["settings"]["max_query_size"] == len(statement) + MAX_QUERY_SIZE_MARGIN
    assert "log_comment" in kwargs["settings"]
