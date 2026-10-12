#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_admin_links.py
#  Purpose:      GET /api/v1/deployment/admin-links - the admin UIs, for admin-class roles only
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The admin UIs a deployment runs, served from what its deployer injected.

The list reaches the app the way a deployment hands it over, through
``deployment.admin_links``, and the one probed link answers from a real local
HTTP server. Roles resolve from the seeded accounts' group membership, so the
gate is checked against the shipped roles rather than a token's claims.
"""

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.admin_links import LINKS_DROPPED
from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.settings import DeploymentSettings, DFESettings
from tests.support.loopback import stop_server

ADMIN_LINKS = "/api/v1/deployment/admin-links"


class _LoginRedirect(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def console() -> Iterator[str]:
    """A serving admin UI: it answers every probe with a login redirect."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _LoginRedirect)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    stop_server(httpd, thread)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _entries(console: str) -> list[dict]:
    return [
        {
            "name": "Argo CD",
            "purpose": "Sync state of every app",
            "url": "https://argocd.dfe.example.com",
            "probe_url": console,
        },
        {
            "name": "Kafbat",
            "purpose": "Topics and consumer lag",
            "url": "https://kafbat.example.com",
        },
        {"name": "Broken", "url": "https://broken.example.com"},
        {
            "name": "Cruise Control",
            "purpose": "Partition rebalancing",
            "url": "https://cruise.example.com",
            "probe_url": f"http://127.0.0.1:{_closed_port()}/",
        },
    ]


@pytest.fixture
def api_settings(api_settings: DFESettings, console: str) -> DFESettings:
    """The shared API settings, carrying the list as the deployer's env var does."""
    return api_settings.model_copy(
        update={"deployment": DeploymentSettings(admin_links=json.dumps(_entries(console)))}
    )


def test_an_admin_gets_each_listed_ui_with_its_status(client: TestClient, admin_headers):
    resp = client.get(ADMIN_LINKS, headers=admin_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {
            "name": "Argo CD",
            "purpose": "Sync state of every app",
            "url": "https://argocd.dfe.example.com",
            "status": "up",
        },
        {
            "name": "Kafbat",
            "purpose": "Topics and consumer lag",
            "url": "https://kafbat.example.com",
            "status": "unknown",
        },
        {
            "name": "Cruise Control",
            "purpose": "Partition rebalancing",
            "url": "https://cruise.example.com",
            "status": "down",
        },
    ]


def test_the_probe_address_never_leaves_the_engine(client: TestClient, admin_headers):
    body = client.get(ADMIN_LINKS, headers=admin_headers).json()

    assert all(set(entry) == {"name", "purpose", "url", "status"} for entry in body)
    assert "127.0.0.1" not in json.dumps(body)


def test_an_infra_admin_gets_the_list(client: TestClient, operator_headers):
    resp = client.get(ADMIN_LINKS, headers=operator_headers)

    assert resp.status_code == 200, resp.text
    assert [entry["name"] for entry in resp.json()] == ["Argo CD", "Kafbat", "Cruise Control"]


def test_a_viewer_is_refused_though_it_holds_deployment_read(client: TestClient, viewer_headers):
    # The fixture viewer carries infra_viewer, whose deployment:read must not reach this.
    assert client.get(ADMIN_LINKS, headers=viewer_headers).status_code == 403


def test_anonymous_is_refused(client: TestClient):
    assert client.get(ADMIN_LINKS).status_code == 401


def _serve(settings: DFESettings, manager=None) -> tuple[int, Any, FastAPI]:
    """One admin GET against an app built from *settings*."""
    app = create_app(settings=settings, metrics_manager=manager)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # The seed issues admin a password it must change; setting it clears that.
            app.state.account_store.reset_password("admin", settings.auth.local.admin_password)
            token = create_access_token(
                data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]}, settings=settings
            )
            resp = client.get(ADMIN_LINKS, headers={"Authorization": f"Bearer {token}"})
    finally:
        _registries.clear()
    return resp.status_code, resp.json(), app


def test_a_deployer_that_lists_nothing_gets_an_empty_list(api_settings: DFESettings):
    status, body, _ = _serve(api_settings.model_copy(update={"deployment": DeploymentSettings()}))

    assert (status, body) == (200, [])


def test_an_unreadable_list_serves_empty_and_is_counted(api_settings: DFESettings):
    manager = create_metrics(
        "test", backend="prometheus", enable_auto_update=False, metric_prefix=""
    )
    broken = api_settings.model_copy(
        update={"deployment": DeploymentSettings(admin_links='[{"name": "Argo CD",')}
    )

    status, body, _ = _serve(broken, manager)

    dropped = {
        sample.labels["reason"]: sample.value
        for family in text_string_to_metric_families(manager.metrics_text)
        for sample in family.samples
        if sample.name == LINKS_DROPPED
    }
    assert (status, body) == (200, [])
    assert dropped == {"unparseable": 1.0}


def test_the_entry_missing_a_purpose_is_dropped_and_counted(api_settings: DFESettings):
    manager = create_metrics(
        "test", backend="prometheus", enable_auto_update=False, metric_prefix=""
    )

    status, _, app = _serve(api_settings, manager)
    invalid = sum(
        sample.value
        for family in text_string_to_metric_families(manager.metrics_text)
        for sample in family.samples
        if sample.name == LINKS_DROPPED and sample.labels["reason"] == "invalid"
    )

    assert status == 200
    assert invalid == 1.0
    assert [link.name for link in app.state.admin_links.links] == [
        "Argo CD",
        "Kafbat",
        "Cruise Control",
    ]
