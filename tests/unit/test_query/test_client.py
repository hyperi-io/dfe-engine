"""Unit tests for QueryClient."""

import http.server
import json
import threading
from typing import ClassVar
from unittest.mock import MagicMock

import pytest
from scalo.cli.openapi import Credential
from scalo.logger import logger

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
        """Create a mock HTTP response returning JSON."""
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
            "has_more": True,
            "next_offset": 3,
            "request_id": "req-1",
        }
        response.raise_for_status = MagicMock()
        return response

    def test_query_http_runs_the_view_not_raw_sql(self, mock_response):
        client = QueryClient(base_url="http://localhost:8000")
        mock_http = MagicMock()
        mock_http.post.return_value = mock_response
        client._http_client = mock_http

        result = client.query("analytics/user_activity")

        mock_http.post.assert_called_once()
        call_args = mock_http.post.call_args
        assert call_args[0][0] == "/api/v1/queries/views/analytics/user_activity/execute"
        assert "query" not in call_args[1]["json"]

        assert result.num_rows == 3
        assert result.metadata.query_duration_ms == 42
        assert result.metadata.has_more is True
        assert result.metadata.next_offset == 3
        assert result.metadata.request_id == "req-1"

    def test_query_http_passes_the_keyset_options(self, mock_response):
        client = QueryClient(base_url="http://localhost:8000")
        mock_http = MagicMock()
        mock_http.post.return_value = mock_response
        client._http_client = mock_http

        client.query(
            "analytics/user_activity",
            order_by="event_ts",
            order_dir="desc",
            after_key="2024-01-15T12:00:00",
            tiebreak_by="event_id",
            after_tiebreak="e-41",
        )

        options = mock_http.post.call_args[1]["json"]["options"]
        assert options["order_by"] == "event_ts"
        assert options["order_dir"] == "desc"
        assert options["after_key"] == "2024-01-15T12:00:00"
        assert options["tiebreak_by"] == "event_id"
        assert options["after_tiebreak"] == "e-41"

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
    def test_direct_mode_requires_view_executor(self, monkeypatch):
        client = QueryClient(direct=True)
        # Hermetic: a local .env that can build a ViewExecutor against a down
        # ClickHouse would otherwise raise ServiceUnavailable instead.
        monkeypatch.setattr(client, "_get_view_executor", lambda: None)
        with pytest.raises(RuntimeError, match="ViewExecutor not available"):
            client.query("analytics/user_activity")


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Answers one view execute and keeps the headers it arrived with."""

    seen: ClassVar[list[dict[str, str]]] = []

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).seen.append(dict(self.headers.items()))
        body = json.dumps({"rows": [], "columns": [], "row_count": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the listener off the test output."""


@pytest.fixture
def engine_url():
    """A real HTTP listener on loopback standing in for the engine's view route."""
    _RecordingHandler.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class TestQueryClientAuthentication:
    @pytest.mark.parametrize(
        ("credential", "header", "value"),
        [
            (Credential(account="ops", token="tok-1"), "Authorization", "Bearer tok-1"),
            (Credential(account="svc", api_key="key-1"), "X-API-Key", "key-1"),
        ],
    )
    def test_http_mode_sends_the_credential_as_the_cli_does(
        self, engine_url, credential, header, value
    ):
        with QueryClient(base_url=engine_url, credential=credential) as client:
            client.query("analytics/user_activity")

        assert _RecordingHandler.seen[0][header] == value

    def test_http_mode_without_a_credential_sends_no_auth_header(self, engine_url):
        with QueryClient(base_url=engine_url) as client:
            client.query("analytics/user_activity")

        sent = {name.lower() for name in _RecordingHandler.seen[0]}
        assert "authorization" not in sent
        assert "x-api-key" not in sent


class TestQueryClientRefusesAnUnsendableCredential:
    @pytest.mark.parametrize(
        "credential",
        [
            Credential(account="ops", token="tok-secret-1\n"),
            Credential(account="ops", token="tok-\rsecret-1"),
            Credential(account="svc", api_key="key-secret-1\x00"),
        ],
    )
    def test_the_error_and_the_log_name_the_fault_and_not_the_value(self, credential):
        lines: list[str] = []
        sink = logger.add(lines.append, level="DEBUG")
        try:
            with pytest.raises(ValueError, match="control character") as refused:
                QueryClient(base_url="http://127.0.0.1:9", credential=credential)
        finally:
            logger.remove(sink)

        assert "secret" not in str(refused.value)
        assert refused.value.__cause__ is None
        assert not [line for line in lines if "secret" in line]
