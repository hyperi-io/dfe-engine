"""Unit tests for datasource adapters."""

from unittest.mock import MagicMock

import pyarrow as pa
import pytest

from dfe_engine.query.datasources import (
    DatasourceAdapter,
    get_adapter,
    list_adapters,
    register_adapter,
)
from dfe_engine.query.datasources.clickhouse import ClickHouseAdapter
from dfe_engine.query.models import ExplainPlan, ExplainStepType


class TestAdapterRegistry:
    """Test datasource adapter registry."""

    def test_list_adapters(self):
        """Test listing registered adapters."""
        adapters = list_adapters()

        assert "clickhouse" in adapters
        assert isinstance(adapters, list)

    def test_get_adapter_clickhouse(self):
        """Test getting ClickHouse adapter."""
        adapter = get_adapter("clickhouse:default")
        assert isinstance(adapter, ClickHouseAdapter)

    def test_get_adapter_with_target(self):
        """Test getting adapter with specific target."""
        adapter = get_adapter("clickhouse:analytics")
        assert adapter.target == "analytics"

    def test_get_adapter_unknown(self):
        """Test error on unknown datasource."""
        with pytest.raises(ValueError, match="Unknown datasource scheme"):
            get_adapter("unknown:target")

    def test_register_adapter_decorator(self):
        """Test registering custom adapter via decorator."""

        @register_adapter("test_custom")
        class CustomAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                return pa.table({"x": [1]})

            def explain(self, query, params=None):
                return ExplainPlan(steps=[], warnings=[])

            def healthcheck(self):
                return True

        assert "test_custom" in list_adapters()

        adapter = get_adapter("test_custom:default")
        assert isinstance(adapter, CustomAdapter)


class TestDatasourceAdapterBase:
    """Test DatasourceAdapter base class."""

    def test_execute_with_explain_sequential(self):
        """Test sequential execute_with_explain."""

        class TestAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                return pa.table({"x": [1, 2, 3]})

            def explain(self, query, params=None):
                return ExplainPlan(
                    steps=[],
                    warnings=["test warning"],
                )

            def healthcheck(self):
                return True

        adapter = TestAdapter("default")
        table, plan = adapter.execute_with_explain("SELECT 1", parallel=False)

        assert table.num_rows == 3
        assert plan.warnings == ["test warning"]

    def test_execute_with_explain_parallel(self):
        """Test parallel execute_with_explain uses threads."""
        call_order = []

        class TestAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                call_order.append("execute")
                return pa.table({"x": [1]})

            def explain(self, query, params=None):
                call_order.append("explain")
                return ExplainPlan(steps=[], warnings=[])

            def healthcheck(self):
                return True

        adapter = TestAdapter("default")
        table, plan = adapter.execute_with_explain("SELECT 1", parallel=True)

        # Both should have been called
        assert "execute" in call_order
        assert "explain" in call_order
        assert table.num_rows == 1


class TestClickHouseAdapter:
    """Test ClickHouse datasource adapter."""

    @pytest.fixture
    def mock_manager(self):
        """Create mock ClickHouseManager."""
        manager = MagicMock()

        # Mock client that returns Arrow
        client = MagicMock()
        client.query_arrow.return_value = pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]})
        client.query.return_value = MagicMock(
            result_rows=[
                ("Expression",),
                ("  ReadFromMergeTree (logs)",),
                ("    Filter",),
            ]
        )

        manager.get_clickhouse_client.return_value = client
        return manager

    @pytest.fixture
    def adapter_with_mock(self, mock_manager):
        """Create adapter with injected mock manager."""
        adapter = ClickHouseAdapter("default")
        adapter._manager = mock_manager
        return adapter

    def test_execute(self, adapter_with_mock, mock_manager):
        """Test ClickHouse query execution."""
        table = adapter_with_mock.execute("SELECT * FROM logs")

        assert table.num_rows == 3
        assert table.column_names == ["id", "name"]

    def test_execute_with_params(self, adapter_with_mock, mock_manager):
        """Test ClickHouse query with parameters."""
        adapter_with_mock.execute(
            "SELECT * FROM logs WHERE id = {id:Int}",
            params={"id": 1},
        )

        # Verify params passed to client
        client = mock_manager.get_clickhouse_client()
        call_kwargs = client.query_arrow.call_args[1]
        assert call_kwargs["parameters"] == {"id": 1}

    def test_execute_with_timeout(self, adapter_with_mock, mock_manager):
        """Test ClickHouse query with timeout."""
        adapter_with_mock.execute("SELECT 1", timeout_seconds=120)

        client = mock_manager.get_clickhouse_client()
        call_kwargs = client.query_arrow.call_args[1]
        assert call_kwargs["settings"]["max_execution_time"] == 120

    def test_explain(self, adapter_with_mock):
        """Test ClickHouse EXPLAIN."""
        plan = adapter_with_mock.explain("SELECT * FROM logs")

        assert isinstance(plan, ExplainPlan)
        assert len(plan.steps) > 0
        assert plan.raw_plan is not None

    def test_healthcheck_success(self, mock_manager):
        """Test successful healthcheck."""
        client = mock_manager.get_clickhouse_client()
        client.query.return_value = MagicMock(result_rows=[(1,)])

        adapter = ClickHouseAdapter("default")
        adapter._manager = mock_manager

        assert adapter.healthcheck() is True

    def test_healthcheck_failure(self, mock_manager):
        """Test failed healthcheck."""
        client = mock_manager.get_clickhouse_client()
        client.query.side_effect = Exception("Connection refused")

        adapter = ClickHouseAdapter("default")
        adapter._manager = mock_manager

        assert adapter.healthcheck() is False

    def test_close(self, mock_manager):
        """Test closing adapter."""
        adapter = ClickHouseAdapter("default")
        adapter._manager = mock_manager

        adapter.close()

        mock_manager.close.assert_called_once()
        assert adapter._manager is None


class TestClickHouseAdapterExplainParsing:
    """Test ClickHouse EXPLAIN plan parsing."""

    def test_classify_step_read(self):
        """Test classifying READ steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        assert adapter._classify_step("ReadFromMergeTree") == ExplainStepType.READ
        assert adapter._classify_step("Read from logs") == ExplainStepType.READ

    def test_classify_step_filter(self):
        """Test classifying FILTER steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        assert adapter._classify_step("Filter (x > 10)") == ExplainStepType.FILTER
        assert adapter._classify_step("Where clause") == ExplainStepType.FILTER
        assert adapter._classify_step("Prewhere") == ExplainStepType.FILTER

    def test_classify_step_aggregate(self):
        """Test classifying AGGREGATE steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        assert adapter._classify_step("Aggregating") == ExplainStepType.AGGREGATE
        assert adapter._classify_step("GroupBy") == ExplainStepType.AGGREGATE

    def test_classify_step_sort(self):
        """Test classifying SORT steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        assert adapter._classify_step("Sorting") == ExplainStepType.SORT
        assert adapter._classify_step("Order by x") == ExplainStepType.SORT

    def test_classify_step_join(self):
        """Test classifying JOIN steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        assert adapter._classify_step("Join") == ExplainStepType.JOIN

    def test_classify_step_projection(self):
        """Test classifying PROJECTION steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        assert adapter._classify_step("Expression") == ExplainStepType.PROJECTION
        assert adapter._classify_step("Project columns") == ExplainStepType.PROJECTION

    def test_classify_step_limit(self):
        """Test classifying LIMIT steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        assert adapter._classify_step("Limit 100") == ExplainStepType.LIMIT

    def test_classify_step_union(self):
        """Test classifying UNION steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        assert adapter._classify_step("Union") == ExplainStepType.UNION

    def test_classify_step_unknown(self):
        """Test classifying unknown steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        assert adapter._classify_step("SomethingElse") == ExplainStepType.UNKNOWN

    def test_extract_details_table(self):
        """Test extracting table name from step."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        details = adapter._extract_details("ReadFromMergeTree(events)")
        assert details is not None
        assert details["table"] == "events"

    def test_extract_details_rows(self):
        """Test extracting row estimate from step."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        details = adapter._extract_details("Filter: ~1000 rows")
        assert details is not None
        assert details["estimated_rows"] == 1000

    def test_extract_details_none(self):
        """Test extraction returns None for plain steps."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        details = adapter._extract_details("Expression")
        assert details is None

    def test_parse_explain_plan(self):
        """Test parsing full EXPLAIN output."""
        adapter = ClickHouseAdapter.__new__(ClickHouseAdapter)

        raw_plan = """Expression
  ReadFromMergeTree (logs)
    Filter (level = 'ERROR')
    Limit 100"""

        steps = adapter._parse_explain_plan(raw_plan)

        assert len(steps) == 4
        assert steps[0].step_type == ExplainStepType.PROJECTION
        assert steps[1].step_type == ExplainStepType.READ
        assert steps[2].step_type == ExplainStepType.FILTER
        assert steps[3].step_type == ExplainStepType.LIMIT


class TestAdapterConfig:
    """Test adapter configuration handling."""

    def test_adapter_with_config(self):
        """Test adapter initialization with config override."""

        class ConfigurableAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                return pa.table({"x": [1]})

            def explain(self, query, params=None):
                return ExplainPlan(steps=[], warnings=[])

            def healthcheck(self):
                return self.config.get("test_key") == "test_value"

        adapter = ConfigurableAdapter(
            "default",
            config={"test_key": "test_value"},
        )

        assert adapter.config["test_key"] == "test_value"
        assert adapter.healthcheck() is True

    def test_adapter_default_config(self):
        """Test adapter with default empty config."""

        class SimpleAdapter(DatasourceAdapter):
            def execute(self, query, params=None, timeout_seconds=30):
                return pa.table({"x": [1]})

            def explain(self, query, params=None):
                return ExplainPlan(steps=[], warnings=[])

            def healthcheck(self):
                return True

        adapter = SimpleAdapter("default")
        assert adapter.config == {}
