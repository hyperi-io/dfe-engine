"""Integration tests for the ClickHouse query adapter against a real ClickHouse.

Runs on the integration harness's ClickHouse (``ch_params``), handing the adapter its
connection through ``config`` the way an explicit caller does. ``QueryClient`` is not
exercised here: it runs labelled parameterised views as a restricted user, never raw SQL.
"""

import csv
import datetime
import io
import json

import pytest

from dfe_engine.clickhouse import ClickHouseManager
from dfe_engine.query.datasources.clickhouse import ClickHouseAdapter
from dfe_engine.query.models import ExplainStepType, QueryMetadata
from dfe_engine.query.result import QueryResult

pytestmark = pytest.mark.integration

NAMES = ["Alice", "Bob", "Charlie", "Diana", "Eve"]


@pytest.fixture
def adapter(ch_params):
    """The adapter on the harness's ClickHouse, through the config a caller passes."""
    # The config lands in the process-wide manager, so no test may inherit another's.
    ClickHouseManager.reset_instance()
    adapter = ClickHouseAdapter(
        "default",
        config={
            "ch_host": ch_params["host"],
            "ch_port": ch_params["port"],
            "ch_username": ch_params["username"],
            "ch_password": ch_params["password"],
            "ch_secure": ch_params["secure"],
        },
    )
    try:
        yield adapter
    finally:
        ClickHouseManager.reset_instance()


@pytest.fixture
def people(ch_client, clickhouse_test_database):
    """Five rows covering scalars, an array, a map and a defaulted DateTime."""
    table = f"`{clickhouse_test_database}`.people"
    ch_client.command(f"""
        CREATE TABLE {table} (
            id UInt64,
            name String,
            value Float64,
            timestamp DateTime DEFAULT now(),
            tags Array(String),
            metadata Map(String, String)
        ) ENGINE = MergeTree()
        ORDER BY id
    """)
    ch_client.command(f"""
        INSERT INTO {table} (id, name, value, tags, metadata)
        VALUES
            (1, 'Alice', 100.5, ['admin', 'user'], {{'org': 'acme', 'role': 'manager'}}),
            (2, 'Bob', 200.0, ['user'], {{'org': 'acme', 'role': 'engineer'}}),
            (3, 'Charlie', 150.25, ['user', 'guest'], {{'org': 'beta', 'role': 'analyst'}}),
            (4, 'Diana', 300.0, ['admin'], {{'org': 'acme', 'role': 'cto'}}),
            (5, 'Eve', 175.5, ['user'], {{'org': 'beta', 'role': 'engineer'}})
    """)
    return table


class TestExecute:
    def test_rows_are_dicts_keyed_by_the_returned_columns(self, adapter, people):
        rows, columns = adapter.execute(f"SELECT * FROM {people} ORDER BY id")
        assert list(columns) == ["id", "name", "value", "timestamp", "tags", "metadata"]
        assert [row["name"] for row in rows] == NAMES
        assert all(list(row) == list(columns) for row in rows)

    def test_a_filter_narrows_the_rows(self, adapter, people):
        rows, _ = adapter.execute(f"SELECT name FROM {people} WHERE value > 150 ORDER BY id")
        assert [row["name"] for row in rows] == ["Bob", "Charlie", "Diana", "Eve"]

    def test_parameters_bind_server_side(self, adapter, people):
        rows, _ = adapter.execute(
            f"SELECT name FROM {people} WHERE name = {{name:String}}", params={"name": "Alice"}
        )
        assert rows == [{"name": "Alice"}]

    def test_a_quote_in_a_parameter_stays_a_value(self, adapter, people):
        rows, _ = adapter.execute(
            f"SELECT count() AS n FROM {people} WHERE name = {{name:String}}",
            params={"name": "Alice' OR '1'='1"},
        )
        assert rows == [{"n": 0}]

    def test_aggregates(self, adapter, people):
        rows, _ = adapter.execute(f"SELECT count() AS cnt, sum(value) AS total FROM {people}")
        assert rows[0]["cnt"] == 5
        assert rows[0]["total"] == pytest.approx(926.25)

    def test_group_by_a_map_key(self, adapter, people):
        rows, _ = adapter.execute(
            f"SELECT metadata['org'] AS org, count() AS cnt FROM {people} "
            "GROUP BY org ORDER BY cnt DESC"
        )
        assert rows == [{"org": "acme", "cnt": 3}, {"org": "beta", "cnt": 2}]

    def test_array_functions(self, adapter, people):
        rows, _ = adapter.execute(f"SELECT name, has(tags, 'admin') AS is_admin FROM {people}")
        admin = {row["name"]: bool(row["is_admin"]) for row in rows}
        assert admin == {"Alice": True, "Bob": False, "Charlie": False, "Diana": True, "Eve": False}

    def test_the_timeout_is_the_servers_execution_limit(self, adapter):
        rows, _ = adapter.execute(
            "SELECT getSetting('max_execution_time') AS limit", timeout_seconds=7
        )
        assert rows[0]["limit"] == 7


class TestShapes:
    def test_an_empty_result_is_no_rows(self, adapter, people):
        rows, _ = adapter.execute(f"SELECT id FROM {people} WHERE id = 999")
        assert rows == []

    def test_a_larger_result_arrives_whole(self, adapter):
        rows, _ = adapter.execute("SELECT number AS id FROM numbers(10000)")
        assert len(rows) == 10000
        assert rows[-1]["id"] == 9999

    def test_null_is_none(self, adapter):
        rows, _ = adapter.execute("SELECT NULL AS null_col, 1 AS int_col")
        assert rows == [{"null_col": None, "int_col": 1}]

    def test_unicode_and_control_characters_round_trip(self, adapter):
        japanese = "".join(map(chr, (0x65E5, 0x672C, 0x8A9E)))
        text = f"{japanese} tab\there newline\nend"
        rows, _ = adapter.execute("SELECT {s:String} AS s", params={"s": text})
        assert rows[0]["s"] == text

    def test_a_datetime_column_is_a_datetime(self, adapter, people):
        rows, _ = adapter.execute(f"SELECT timestamp FROM {people} LIMIT 1")
        assert isinstance(rows[0]["timestamp"], datetime.datetime)


class TestExplain:
    def test_the_plan_reads_the_table(self, adapter, people):
        plan = adapter.explain(f"SELECT * FROM {people} WHERE value > 100")
        assert plan.raw_plan
        assert ExplainStepType.READ in [step.step_type for step in plan.steps]

    def test_an_aggregation_plans_an_aggregate_step(self, adapter, people):
        plan = adapter.explain(f"SELECT metadata['org'], count() FROM {people} GROUP BY 1")
        assert ExplainStepType.AGGREGATE in [step.step_type for step in plan.steps]

    @pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
    def test_execute_with_explain(self, adapter, people, parallel):
        rows, columns, plan = adapter.execute_with_explain(
            f"SELECT id, name FROM {people}", parallel=parallel
        )
        assert len(rows) == 5
        assert list(columns) == ["id", "name"]
        assert plan.steps


def test_healthcheck(adapter):
    assert adapter.healthcheck() is True


class TestExportsOfRealRows:
    """The export paths get real ClickHouse types: DateTime, Array and Map."""

    @pytest.fixture
    def result(self, adapter, people):
        rows, columns = adapter.execute(f"SELECT * FROM {people} ORDER BY id")
        metadata = QueryMetadata(
            row_count=len(rows), query_duration_ms=0, query_label="people", datasource="clickhouse"
        )
        return QueryResult(rows=rows, columns=columns, metadata=metadata)

    def test_json(self, result):
        data = json.loads(result.to_json())
        assert [row["name"] for row in data] == NAMES
        assert data[0]["tags"] == ["admin", "user"]
        assert data[0]["metadata"] == {"org": "acme", "role": "manager"}

    def test_csv(self, result):
        rows = list(csv.DictReader(io.StringIO(result.to_csv())))
        assert [row["name"] for row in rows] == NAMES
        assert list(rows[0]) == list(result.columns)
