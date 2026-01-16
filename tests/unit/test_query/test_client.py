"""Unit tests for QueryClient with labeled queries."""

from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

from dfe_engine.query.client import QueryClient
from dfe_engine.query.models import (
    ExplainPlan,
    ExplainStep,
    ExplainStepType,
    ParameterDefinition,
    ParameterType,
    QueryDefinition,
    QueryMetadata,
)
from dfe_engine.query.result import QueryResult


class TestQueryClientInit:
    """Test QueryClient initialization."""

    def test_init_http_mode(self):
        """Test initialization with HTTP base URL."""
        client = QueryClient(base_url="http://localhost:8000")

        assert client.base_url == "http://localhost:8000"
        assert client.direct is False
        assert client.timeout_seconds == 30

    def test_init_direct_mode(self):
        """Test initialization in direct mode."""
        client = QueryClient(direct=True)

        assert client.direct is True
        assert client.base_url is None

    def test_init_custom_timeout(self):
        """Test initialization with custom timeout."""
        client = QueryClient(base_url="http://localhost:8000", timeout_seconds=120)

        assert client.timeout_seconds == 120

    def test_init_requires_base_url_or_direct(self):
        """Test that either base_url or direct must be specified."""
        with pytest.raises(ValueError, match="Must specify base_url or direct"):
            QueryClient()

    def test_context_manager(self):
        """Test context manager protocol."""
        with QueryClient(base_url="http://localhost:8000") as client:
            assert client.base_url == "http://localhost:8000"

        # After context exit, client should be cleaned up
        assert client._http_client is None


class TestQueryClientDirectMode:
    """Test QueryClient direct mode execution with labeled queries."""

    @pytest.fixture
    def sample_query_def(self):
        """Create a sample query definition."""
        return QueryDefinition(
            datasource="clickhouse:default",
            store="events",
            sql="""
                SELECT id, name, count() as cnt
                FROM {{ store }}.logs
                WHERE org_id = {{ _org_id }}
                {% if event_type %}AND event_type = {{ event_type | sql_string }}{% endif %}
                LIMIT {{ limit }}
            """,
            parameters={
                "event_type": ParameterDefinition(
                    type=ParameterType.STRING,
                    required=False,
                    description="Filter by event type",
                )
            },
            tenant_isolated=True,
        )

    @pytest.fixture
    def mock_registry(self, sample_query_def):
        """Create a mock registry."""
        registry = MagicMock()
        registry.get.return_value = sample_query_def
        registry.resolve_store.return_value = ["events"]
        registry.render_sql.return_value = """
            SELECT id, name, count() as cnt
            FROM events.logs
            WHERE org_id = 'direct'
            LIMIT 1000
        """
        return registry

    @pytest.fixture
    def mock_adapter(self):
        """Create a mock datasource adapter."""
        adapter = MagicMock()
        adapter.execute.return_value = pa.table(
            {"id": [1, 2, 3], "name": ["a", "b", "c"], "cnt": [10, 20, 30]}
        )
        adapter.explain.return_value = ExplainPlan(
            steps=[
                ExplainStep(step_type=ExplainStepType.READ, description="ReadFromMergeTree")
            ],
            warnings=[],
        )
        adapter.execute_with_explain.return_value = (
            pa.table({"id": [1, 2, 3]}),
            ExplainPlan(steps=[], warnings=[]),
        )
        return adapter

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_query_direct_basic(
        self, mock_validate, mock_get_adapter, mock_get_registry, mock_registry, mock_adapter
    ):
        """Test basic direct mode query execution."""
        mock_get_registry.return_value = mock_registry
        mock_get_adapter.return_value = mock_adapter
        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "limit": 1000,
            "offset": 0,
            "timeout_seconds": 30,
        }

        client = QueryClient(direct=True)
        result = client.query("analytics/user_activity")

        # Verify registry was queried
        mock_get_registry.assert_called_once()
        mock_registry.get.assert_called_once_with("analytics/user_activity")

        # Verify result
        assert isinstance(result, QueryResult)
        assert result.table.num_rows == 3

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_query_with_params(
        self, mock_validate, mock_get_adapter, mock_get_registry, mock_registry, mock_adapter
    ):
        """Test query with parameters."""
        mock_get_registry.return_value = mock_registry
        mock_get_adapter.return_value = mock_adapter
        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "event_type": "login",
            "limit": 500,
            "offset": 0,
            "timeout_seconds": 30,
        }

        client = QueryClient(direct=True)
        result = client.query(
            "analytics/user_activity",
            params={"event_type": "login"},
            limit=500,
        )

        # Verify params were passed to validator
        call_args = mock_validate.call_args
        assert call_args[0][1] == {"event_type": "login"}  # client_params

        assert result.table.num_rows == 3

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_query_df(
        self, mock_validate, mock_get_adapter, mock_get_registry, mock_registry, mock_adapter
    ):
        """Test query returning DataFrame."""
        mock_get_registry.return_value = mock_registry
        mock_get_adapter.return_value = mock_adapter
        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "limit": 1000,
            "offset": 0,
            "timeout_seconds": 30,
        }

        client = QueryClient(direct=True)
        df = client.query_df("analytics/user_activity")

        assert len(df) == 3
        assert list(df.columns) == ["id", "name", "cnt"]

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_query_with_explain(
        self, mock_validate, mock_get_adapter, mock_get_registry, mock_registry, mock_adapter
    ):
        """Test query with EXPLAIN plan."""
        mock_get_registry.return_value = mock_registry
        mock_get_adapter.return_value = mock_adapter
        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "limit": 1000,
            "offset": 0,
            "timeout_seconds": 30,
        }

        client = QueryClient(direct=True)
        result = client.query_with_explain(
            "analytics/user_activity",
            parallel=True,
        )

        mock_adapter.execute_with_explain.assert_called_once()
        assert result.explain is not None

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_query_batches(
        self, mock_validate, mock_get_adapter, mock_get_registry, mock_registry, mock_adapter
    ):
        """Test streaming query as batches."""
        mock_get_registry.return_value = mock_registry
        mock_get_adapter.return_value = mock_adapter
        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "limit": 1000,
            "offset": 0,
            "timeout_seconds": 30,
        }

        client = QueryClient(direct=True)
        batches = list(
            client.query_batches("analytics/user_activity", batch_size=2)
        )

        assert len(batches) >= 1
        total_rows = sum(b.num_rows for b in batches)
        assert total_rows == 3

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_query_with_store(
        self, mock_validate, mock_get_adapter, mock_get_registry, mock_registry, mock_adapter
    ):
        """Test query with client-specified store."""
        mock_get_registry.return_value = mock_registry
        mock_get_adapter.return_value = mock_adapter
        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "limit": 1000,
            "offset": 0,
            "timeout_seconds": 30,
        }

        client = QueryClient(direct=True)
        client.query("analytics/user_activity", store="custom_db")

        # Verify options included store
        call_args = mock_validate.call_args
        options = call_args[0][2]  # QueryOptions
        assert options.store == "custom_db"

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_query_with_time_bounds(
        self, mock_validate, mock_get_adapter, mock_get_registry, mock_registry, mock_adapter
    ):
        """Test query with time bounds."""
        mock_get_registry.return_value = mock_registry
        mock_get_adapter.return_value = mock_adapter
        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "limit": 1000,
            "offset": 0,
            "timeout_seconds": 30,
            "time_from": "2024-01-01T00:00:00Z",
            "time_to": "2024-01-31T23:59:59Z",
        }

        client = QueryClient(direct=True)
        client.query(
            "analytics/user_activity",
            time_from="2024-01-01T00:00:00Z",
            time_to="2024-01-31T23:59:59Z",
        )

        call_args = mock_validate.call_args
        options = call_args[0][2]  # QueryOptions
        assert options.time_from == "2024-01-01T00:00:00Z"
        assert options.time_to == "2024-01-31T23:59:59Z"


class TestQueryClientHttpMode:
    """Test QueryClient HTTP mode execution."""

    @pytest.fixture
    def mock_response(self):
        """Create a mock HTTP response."""
        # Create Arrow IPC bytes
        table = pa.table({"id": [1, 2, 3], "value": ["a", "b", "c"]})
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        ipc_bytes = sink.getvalue().to_pybytes()

        response = MagicMock()
        response.status_code = 200
        response.content = ipc_bytes
        response.headers = {
            "X-Row-Count": "3",
            "X-Query-Duration-Ms": "42",
            "X-Datasource": "clickhouse:default",
            "X-Store": "events",
            "X-Truncated": "false",
            "X-Cached": "false",
            "X-Request-Id": "req-123",
        }
        response.raise_for_status = MagicMock()
        return response

    def test_query_http(self, mock_response):
        """Test HTTP mode query execution."""
        client = QueryClient(base_url="http://localhost:8000")
        mock_http = MagicMock()
        mock_http.post.return_value = mock_response
        client._http_client = mock_http

        result = client.query("analytics/user_activity")

        # Verify HTTP call
        mock_http.post.assert_called_once()
        call_args = mock_http.post.call_args
        assert call_args[0][0] == "/api/v1/query"
        assert call_args[1]["json"]["query"] == "analytics/user_activity"

        # Verify result
        assert result.table.num_rows == 3
        assert result.metadata.query_duration_ms == 42

    def test_query_http_with_params(self, mock_response):
        """Test HTTP mode query with parameters."""
        client = QueryClient(base_url="http://localhost:8000")
        mock_http = MagicMock()
        mock_http.post.return_value = mock_response
        client._http_client = mock_http

        client.query(
            "analytics/user_activity",
            params={"event_type": "login"},
            limit=500,
        )

        call_args = mock_http.post.call_args
        json_body = call_args[1]["json"]
        assert json_body["params"] == {"event_type": "login"}
        assert json_body["options"]["limit"] == 500

    def test_query_http_with_explain(self, mock_response):
        """Test HTTP mode extracts EXPLAIN from response."""
        # Add explain metadata to response
        table = pa.table({"id": [1]})
        explain_metadata = {
            b"dfe:explain:steps": b'[{"step_type": "read", "description": "test"}]',
            b"dfe:explain:warnings": b"[]",
        }
        schema_with_meta = table.schema.with_metadata(explain_metadata)

        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, schema_with_meta) as writer:
            writer.write_table(table.cast(schema_with_meta))
        ipc_bytes = sink.getvalue().to_pybytes()

        mock_response = MagicMock()
        mock_response.content = ipc_bytes
        mock_response.headers = {
            "X-Row-Count": "1",
            "X-Query-Duration-Ms": "10",
            "X-Query-Label": "analytics/user_activity",
            "X-Datasource": "clickhouse:default",
            "X-Truncated": "false",
            "X-Cached": "false",
            "X-Explain-Duration-Ms": "5",
        }
        mock_response.raise_for_status = MagicMock()

        client = QueryClient(base_url="http://localhost:8000")
        mock_http = MagicMock()
        mock_http.post.return_value = mock_response
        client._http_client = mock_http

        result = client.query_with_explain("analytics/user_activity")

        assert result.explain is not None
        assert result.metadata.explain_duration_ms == 5


class TestQueryClientMetadataParsing:
    """Test parsing of response metadata."""

    def test_metadata_from_headers(self):
        """Test parsing metadata from HTTP headers."""
        metadata = QueryMetadata(
            row_count=1000,
            query_duration_ms=150,
            query_label="analytics/user_activity",
            datasource="clickhouse:default",
            store="events",
            truncated=True,
            cached=True,
            explain_duration_ms=25,
            request_id="req-123",
        )

        assert metadata.row_count == 1000
        assert metadata.query_duration_ms == 150
        assert metadata.query_label == "analytics/user_activity"
        assert metadata.truncated is True
        assert metadata.cached is True
        assert metadata.explain_duration_ms == 25


class TestQueryClientErrorHandling:
    """Test error handling scenarios."""

    @patch("dfe_engine.query.registry.get_registry")
    def test_query_not_found(self, mock_get_registry):
        """Test handling query not found error."""
        from dfe_engine.query.registry import QueryNotFoundError

        registry = MagicMock()
        registry.get.side_effect = QueryNotFoundError("Query not found: invalid/query")
        mock_get_registry.return_value = registry

        client = QueryClient(direct=True)

        with pytest.raises(QueryNotFoundError, match="Query not found"):
            client.query("invalid/query")

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.validator.validate_params")
    def test_validation_error(self, mock_validate, mock_get_registry):
        """Test handling parameter validation error."""
        from dfe_engine.query.validator import ParameterValidationError

        registry = MagicMock()
        registry.get.return_value = MagicMock()
        mock_get_registry.return_value = registry
        mock_validate.side_effect = ParameterValidationError(
            "Missing required parameter: severity"
        )

        client = QueryClient(direct=True)

        with pytest.raises(ParameterValidationError, match="Missing required parameter"):
            client.query("hunts/active_threats")

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.validator.validate_params")
    def test_authorization_error(self, mock_validate, mock_get_registry):
        """Test handling authorization error."""
        from dfe_engine.query.validator import AuthorizationError

        registry = MagicMock()
        registry.get.return_value = MagicMock()
        mock_get_registry.return_value = registry
        mock_validate.side_effect = AuthorizationError("Requires one of roles: [admin]")

        client = QueryClient(direct=True)

        with pytest.raises(AuthorizationError, match="Requires one of roles"):
            client.query("admin/system_stats")

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_execution_error(
        self, mock_validate, mock_get_adapter, mock_get_registry
    ):
        """Test handling query execution errors."""
        registry = MagicMock()
        registry.get.return_value = MagicMock(datasource="clickhouse:default", store="events")
        registry.resolve_store.return_value = ["events"]
        registry.render_sql.return_value = "SELECT 1"
        mock_get_registry.return_value = registry

        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "limit": 1000,
            "offset": 0,
            "timeout_seconds": 30,
        }

        adapter = MagicMock()
        adapter.execute.side_effect = RuntimeError("Connection refused")
        mock_get_adapter.return_value = adapter

        client = QueryClient(direct=True)

        with pytest.raises(RuntimeError, match="Connection refused"):
            client.query("analytics/user_activity")


class TestQueryClientParallelExplain:
    """Test parallel EXPLAIN execution."""

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_parallel_flag_passed(
        self, mock_validate, mock_get_adapter, mock_get_registry
    ):
        """Test parallel flag is passed to adapter."""
        registry = MagicMock()
        registry.get.return_value = MagicMock(datasource="clickhouse:default", store="events")
        registry.resolve_store.return_value = ["events"]
        registry.render_sql.return_value = "SELECT 1"
        mock_get_registry.return_value = registry

        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "limit": 1000,
            "offset": 0,
            "timeout_seconds": 30,
        }

        adapter = MagicMock()
        adapter.execute_with_explain.return_value = (
            pa.table({"x": [1]}),
            ExplainPlan(steps=[], warnings=[]),
        )
        mock_get_adapter.return_value = adapter

        client = QueryClient(direct=True)
        client.query_with_explain("analytics/user_activity", parallel=True)

        call_kwargs = adapter.execute_with_explain.call_args[1]
        assert call_kwargs.get("parallel") is True

    @patch("dfe_engine.query.registry.get_registry")
    @patch("dfe_engine.query.datasources.get_adapter")
    @patch("dfe_engine.query.validator.validate_params")
    def test_sequential_explain(
        self, mock_validate, mock_get_adapter, mock_get_registry
    ):
        """Test sequential (non-parallel) EXPLAIN execution."""
        registry = MagicMock()
        registry.get.return_value = MagicMock(datasource="clickhouse:default", store="events")
        registry.resolve_store.return_value = ["events"]
        registry.render_sql.return_value = "SELECT 1"
        mock_get_registry.return_value = registry

        mock_validate.return_value = {
            "_org_id": "direct",
            "_user_id": "direct",
            "_roles": ["admin"],
            "_request_id": "test-123",
            "limit": 1000,
            "offset": 0,
            "timeout_seconds": 30,
        }

        adapter = MagicMock()
        adapter.execute_with_explain.return_value = (
            pa.table({"x": [1]}),
            ExplainPlan(steps=[], warnings=[]),
        )
        mock_get_adapter.return_value = adapter

        client = QueryClient(direct=True)
        client.query_with_explain("analytics/user_activity", parallel=False)

        call_kwargs = adapter.execute_with_explain.call_args[1]
        assert call_kwargs.get("parallel") is False
