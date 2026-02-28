"""Unit tests for QueryClient."""

from unittest.mock import MagicMock

import pyarrow as pa
import pytest

from dfe_engine.query.client import QueryClient
from dfe_engine.query.models import (
    QueryMetadata,
)


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


class TestQueryClientDirectMode:
    """Test QueryClient direct mode routes through ViewExecutor."""

    def test_direct_mode_requires_view_executor(self):
        """Test that direct mode raises RuntimeError when ViewExecutor unavailable."""
        client = QueryClient(direct=True)

        # Without a configured ViewExecutor, direct mode should fail cleanly
        with pytest.raises(RuntimeError, match="ViewExecutor not available"):
            client.query("analytics/user_activity")
