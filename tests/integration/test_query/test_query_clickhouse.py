"""Integration tests for the ClickHouse query adapter against a real ClickHouse.

Runs on the integration harness's ClickHouse (``ch_params``), handing the adapter its
connection through ``config`` the way an explicit caller does. ``QueryClient`` is not
exercised here: it runs labelled parameterised views as a restricted user, never raw SQL.
"""

import csv
import datetime
import io
import json
import socket

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from dfe_engine.clickhouse import ClickHouseManager
from dfe_engine.query.datasources.clickhouse import ClickHouseAdapter
from dfe_engine.query.models import ExplainStepType, QueryMetadata
from dfe_engine.query.result import QueryResult

pytestmark = pytest.mark.integration

NAMES = ["Alice", "Bob", "Charlie", "Diana", "Eve"]


def _config(ch_params) -> dict:
    """The manager config for the harness's ClickHouse."""
    return {
        "ch_host": ch_params["host"],
        "ch_port": ch_params["port"],
        "ch_username": ch_params["username"],
        "ch_password": ch_params["password"],
        "ch_secure": ch_params["secure"],
    }


@pytest.fixture
def adapter(ch_params):
    """The adapter on the harness's ClickHouse, through the config a caller passes."""
    adapter = ClickHouseAdapter("default", config=_config(ch_params))
    try:
        yield adapter
    finally:
        adapter.close()


@pytest.fixture
def shared_adapter(ch_params):
    """An adapter with no config, on the process-wide manager the engine seeds at startup."""
    ClickHouseManager.reset_instance()
    ClickHouseManager.get_instance(_config(ch_params))
    try:
        yield ClickHouseAdapter("default")
    finally:
        ClickHouseManager.reset_instance()


def _refused_port() -> int:
    """A local port nothing listens on: bound once for its number, then released."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


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
        assert columns == ["id", "name", "value", "timestamp", "tags", "metadata"]
        assert [row["name"] for row in rows] == NAMES
        assert all(list(row) == columns for row in rows)

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


READONLY_REFUSAL = "Code: 164"

# ClickHouse's own HTTP port, as the server itself reaches it.
SELF_URL = "http://127.0.0.1:8123/?query="


class TestReadOnly:
    """The adapter runs as the engine's own user, which may write, so it sends readonly=1."""

    def test_a_plain_select_runs_read_only_with_its_timeout(self, adapter, people):
        rows, _ = adapter.execute(
            f"SELECT count() AS n, getSetting('readonly') AS ro, "
            f"getSetting('max_execution_time') AS limit FROM {people}",
            timeout_seconds=11,
        )
        assert rows == [{"n": 5, "ro": 1, "limit": 11}]

    def test_insert_into_function_url_is_refused(self, adapter, ch_client, people):
        target = f"INSERT%20INTO%20{people.replace('`', '')}%20(id)%20FORMAT%20TSV"
        with pytest.raises(DatabaseError, match=READONLY_REFUSAL):
            adapter.execute(
                f"INSERT INTO FUNCTION url('{SELF_URL}{target}', 'TSV', 'id UInt64') VALUES (97)"
            )
        assert ch_client.query(f"SELECT count() FROM {people}").result_rows == [(5,)]

    def test_insert_into_function_file_is_refused(self, adapter):
        with pytest.raises(DatabaseError, match=READONLY_REFUSAL):
            adapter.execute(
                "INSERT INTO FUNCTION file('dfe_readonly_probe.tsv', 'TSV', 'x String') "
                "VALUES ('written')"
            )

    def test_a_read_through_url_is_refused(self, adapter):
        with pytest.raises(DatabaseError, match=READONLY_REFUSAL):
            adapter.execute(f"SELECT * FROM url('{SELF_URL}SELECT%201', 'TSV', 'x String')")

    def test_a_create_is_refused(self, adapter, clickhouse_test_database):
        with pytest.raises(DatabaseError, match="readonly"):
            adapter.execute(
                f"CREATE TABLE `{clickhouse_test_database}`.made (x UInt8) ENGINE = Log"
            )

    def test_an_insert_is_refused_and_writes_nothing(self, adapter, ch_client, people):
        with pytest.raises(DatabaseError, match="readonly"):
            adapter.execute(f"INSERT INTO {people} (id, name, value) VALUES (99, 'Z', 0)")
        assert ch_client.query(f"SELECT count() FROM {people}").result_rows == [(5,)]

    def test_a_drop_is_refused(self, adapter, ch_client, people):
        with pytest.raises(DatabaseError, match="readonly"):
            adapter.execute(f"DROP TABLE {people}")
        assert ch_client.query(f"SELECT count() FROM {people}").result_rows == [(5,)]

    def test_a_query_cannot_lift_the_restriction(self, adapter, ch_client, people):
        with pytest.raises(DatabaseError, match="readonly"):
            adapter.execute(
                f"INSERT INTO {people} (id, name, value) SETTINGS readonly = 0 VALUES (98, 'Y', 0)"
            )
        with pytest.raises(DatabaseError, match="readonly"):
            adapter.execute("SELECT 1 SETTINGS readonly = 0")
        assert ch_client.query(f"SELECT count() FROM {people}").result_rows == [(5,)]

    def test_a_settings_clause_is_refused(self, adapter):
        with pytest.raises(DatabaseError, match=READONLY_REFUSAL):
            adapter.execute("SELECT getSetting('max_threads') AS t SETTINGS max_threads = 3")


class TestAnEmptyResult:
    """ClickHouse sends no Native block for zero rows, so the names come from DESCRIBE."""

    def test_still_names_its_columns(self, adapter, people):
        assert adapter.execute(f"SELECT id, name FROM {people} WHERE id = 999") == (
            [],
            ["id", "name"],
        )

    def test_binds_its_parameters_to_name_the_columns(self, adapter, people):
        rows, columns = adapter.execute(
            f"SELECT name FROM {people} WHERE name = {{name:String}}", params={"name": "nobody"}
        )
        assert (rows, columns) == ([], ["name"])

    def test_names_its_columns_through_a_trailing_semicolon_and_comment(self, adapter, people):
        rows, columns = adapter.execute(f"SELECT id FROM {people} WHERE id = 999 -- none\n;")
        assert (rows, columns) == ([], ["id"])

    def test_a_statement_describe_cannot_wrap_has_no_columns(
        self, adapter, clickhouse_test_database
    ):
        assert adapter.execute(f"SHOW TABLES FROM `{clickhouse_test_database}`") == ([], [])


class TestShapes:
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
        assert columns == ["id", "name"]
        assert plan.steps

    def test_the_estimate_counts_rows_not_parts(self, adapter, ch_client, people):
        # A second insert makes a second part: 8 rows across 2 parts.
        ch_client.command(
            f"INSERT INTO {people} (id, name, value) VALUES (6, 'F', 1), (7, 'G', 2), (8, 'H', 3)"
        )
        assert adapter.explain(f"SELECT * FROM {people}").total_estimated_rows == 8

    def test_a_query_that_reads_no_mergetree_table_has_no_estimate(self, adapter):
        assert adapter.explain("SELECT 1").total_estimated_rows is None


def test_healthcheck(adapter):
    assert adapter.healthcheck() is True


class TestTheManagerAnAdapterUses:
    def test_two_configured_adapters_reach_their_own_servers(self, ch_params):
        harness = ClickHouseAdapter("default", config=_config(ch_params))
        refused = {**_config(ch_params), "ch_host": "127.0.0.1", "ch_port": _refused_port()}
        elsewhere = ClickHouseAdapter("default", config=refused)
        try:
            assert harness.manager.ping() is True
            assert elsewhere.manager.ping() is False
        finally:
            harness.close()
            elsewhere.close()

    def test_closing_a_configured_adapter_closes_its_own_client(self, adapter):
        adapter.execute("SELECT 1")
        owned = adapter.manager
        adapter.close()
        assert owned._client is None
        assert adapter.execute("SELECT 2 AS n") == ([{"n": 2}], ["n"])

    def test_an_adapter_with_no_config_uses_the_shared_manager(self, shared_adapter):
        assert shared_adapter.manager is ClickHouseManager.get_instance()

    def test_closing_it_leaves_the_shared_client_open(self, shared_adapter):
        shared_adapter.execute("SELECT 1")
        shared_adapter.close()
        shared = ClickHouseManager.get_instance().get_clickhouse_client()
        assert shared.query("SELECT 1").result_rows == [(1,)]
        assert shared_adapter.execute("SELECT 2 AS n") == ([{"n": 2}], ["n"])


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
        assert list(rows[0]) == result.columns
