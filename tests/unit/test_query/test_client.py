"""Unit tests for QueryClient."""

from unittest.mock import MagicMock

import pytest

from dfe_engine.query.client import QueryClient
from dfe_engine.query.models import QueryMetadata


class TestQueryClientInit:
    def test_init_http_mode(self):
        client = QueryClient(base_url="http://localhost:8000")
        assert client.base_url == "http://localhost:8000"
        assert client.direct is False
        assert client.timeout_seconds == 30

    def test_init_direct_mode(self):
        client = QueryClient(direct=True)
        assert client.direct is True
        assert client.base_url is None

    def test_init_custom_timeout(self):
        client = QueryClient(base_url="http://localhost:8000", timeout_seconds=120)
        assert client.timeout_seconds == 120

    def test_init_requires_base_url_or_direct(self):
        with pytest.raises(ValueError, match="Must specify base_url or direct"):
            QueryClient()

    def test_context_manager(self):
        with QueryClient(base_url="http://localhost:8000") as client:
            assert client.base_url == "http://localhost:8000"
        assert client._http_client is None


class TestQueryClientHttpMode:
    @pytest.fixture
    def mock_response(self):
        """Mock HTTP response shaped like the view-execute QueryResponse JSON."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "rows": [
                {"id": 1, "value": "a"},
                {"id": 2, "value": "b"},
                {"id": 3, "value": "c"},
            ],
            "columns": ["id", "value"],
            "row_count": 3,
            "query_duration_ms": 42,
        }
        response.headers = {}
        response.raise_for_status = MagicMock()
        return response

    def test_query_http_uses_view_execute_path(self, mock_response):
        # HTTP mode must hit the tenant view-execute endpoint with the label in
        # the path, NOT post the label as SQL to /queries/raw.
        client = QueryClient(base_url="http://localhost:8000")
        mock_http = MagicMock()
        mock_http.post.return_value = mock_response
        client._http_client = mock_http

        result = client.query("analytics/user_activity")

        mock_http.post.assert_called_once()
        call_args = mock_http.post.call_args
        assert call_args[0][0] == "/api/v1/queries/views/analytics/user_activity/execute"
        json_body = call_args[1]["json"]
        assert "query" not in json_body
        assert "datasource" not in json_body
        assert set(json_body) == {"params", "options"}

        assert result.num_rows == 3
        assert result.metadata.query_duration_ms == 42

    def test_query_http_with_params(self, mock_response):
        client = QueryClient(base_url="http://localhost:8000")
        mock_http = MagicMock()
        mock_http.post.return_value = mock_response
        client._http_client = mock_http

        client.query("analytics/user_activity", params={"event_type": "login"}, limit=500)

        call_args = mock_http.post.call_args
        json_body = call_args[1]["json"]
        assert json_body["params"] == {"event_type": "login"}
        assert json_body["options"]["limit"] == 500


class TestQueryClientMetadataParsing:
    def test_metadata_from_headers(self):
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
        assert metadata.truncated is True
        assert metadata.cached is True


class TestQueryClientDirectMode:
    def test_direct_mode_requires_view_executor(self):
        client = QueryClient(direct=True)
        with pytest.raises(RuntimeError, match="ViewExecutor not available"):
            client.query("analytics/user_activity")
