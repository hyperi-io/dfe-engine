#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_view_executor_wiring.py
#  Purpose:      The view routes build their executor on first use, as the restricted reader
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The view routes build their executor on demand, and say why when they cannot.

These fixtures' secrets store is empty and no reader password is configured, so the
build stops before any connection is opened. The rows-returned path needs a real
ClickHouse and lives in tests/integration/test_query/test_views_api.py.
"""

import pytest

EXECUTE = "/api/v1/queries/views/analytics/events/execute"


@pytest.fixture(autouse=True)
def _no_reader_password(monkeypatch):
    monkeypatch.delenv("DFE_CLICKHOUSE_QUERY_READER_PASSWORD", raising=False)
    monkeypatch.delenv("DFE_QUERY_VIEWS_RESTRICTED_PASSWORD", raising=False)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", EXECUTE),
        ("GET", "/api/v1/queries/views"),
        ("GET", "/api/v1/queries/views/namespaces"),
    ],
)
def test_a_reader_with_no_password_is_a_503_that_names_it(client, admin_headers, method, path):
    body = {} if method == "POST" else None
    resp = client.request(method, path, headers=admin_headers, json=body)

    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "reader_unprovisioned"
    assert "dfe_query_reader" in resp.json()["message"]


def test_a_failed_build_is_not_kept(app, client, admin_headers):
    client.post(EXECUTE, headers=admin_headers, json={})

    assert getattr(app.state, "view_executor", None) is None


def test_a_clickhouse_it_cannot_reach_is_a_503_not_configured(
    app, client, admin_headers, api_settings, unused_tcp_port
):
    """With a password but nothing listening, the build fails on the connect."""
    api_settings.query_views.restricted_password = "reader-pw"
    api_settings.clickhouse.host = "127.0.0.1"
    api_settings.clickhouse.port = unused_tcp_port

    resp = client.post(EXECUTE, headers=admin_headers, json={})

    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "not_configured"
    assert getattr(app.state, "view_executor", None) is None
