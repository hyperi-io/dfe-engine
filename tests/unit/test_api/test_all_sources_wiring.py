#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_all_sources_wiring.py
#  Purpose:      A source delete rebuilds all_sources, and startup registers its HyperDX source
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A source delete rebuilds all_sources without failing on it, and startup registers its HyperDX source in the background.

A unit test has no ClickHouse: the conftest guard refuses the admin client, so a rebuild reaches the ClickHouse call and fails the way a down server would.
"""

import socket
import time
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.settings import DFESettings, HyperDXSettings

GUARD_REFUSAL = "unit tests do not reconcile against a ClickHouse (conftest guard)"
NOT_REBUILT = "all_sources not rebuilt"
NOT_REGISTERED = "HyperDX all_sources source not registered; trying again until it is"


def _create_source(*, client: TestClient, headers: dict[str, str], name: str) -> None:
    """Create a source named *name* through the API."""
    body = {"match": {"field": "tags.collector.type", "value": name}, "source": name}
    client.post("/api/v1/sources", headers=headers, json=body)


def _rebuilds(*, events: list[dict]) -> list[dict]:
    """The rebuild failures logged, with their fields."""
    return [entry for entry in events if entry["event"] == NOT_REBUILT]


def _refused_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        return spare.getsockname()[1]


def _wait_for(*, condition: Callable[[], bool]) -> bool:
    """Poll *condition* for up to five seconds and report whether it came true."""
    for _attempt in range(500):
        if condition():
            return True
        time.sleep(0.01)
    return False


class TestDeleteSource:
    def test_rebuilds_all_sources_without_failing_the_delete(
        self, admin_headers: dict, audit_events: list[dict], client: TestClient
    ):
        _create_source(client=client, headers=admin_headers, name="zeek")

        response = client.delete("/api/v1/sources/zeek", headers=admin_headers)

        assert (response.status_code, _rebuilds(events=audit_events)) == (
            204,
            [
                {
                    "event": NOT_REBUILT,
                    "changed": ["zeek"],
                    "database": "dfe",
                    "error": GUARD_REFUSAL,
                }
            ],
        )


class TestBulkAction:
    def test_a_bulk_delete_rebuilds_all_sources_once(
        self, admin_headers: dict, audit_events: list[dict], client: TestClient
    ):
        for name in ("okta", "zeek"):
            _create_source(client=client, headers=admin_headers, name=name)

        response = client.post(
            "/api/v1/sources/bulk",
            headers=admin_headers,
            json={"action": "delete", "sources": ["okta", "zeek"]},
        )

        assert (response.status_code, _rebuilds(events=audit_events)) == (
            200,
            [
                {
                    "event": NOT_REBUILT,
                    "changed": ["okta", "zeek"],
                    "database": "dfe",
                    "error": GUARD_REFUSAL,
                }
            ],
        )


class TestLifespan:
    def test_without_hyperdx_registers_nothing(self, app: FastAPI, client: TestClient):
        assert getattr(app.state, "all_sources_registration", None) is None

    def test_registers_all_sources_while_hyperdx_is_down(
        self, api_settings: DFESettings, audit_events: list[dict]
    ):
        def warned() -> bool:
            return any(entry["event"] == NOT_REGISTERED for entry in audit_events)

        api_settings.hyperdx = HyperDXSettings(
            base_url=f"http://127.0.0.1:{_refused_port()}", enabled=True
        )
        application = create_app(settings=api_settings)
        try:
            with TestClient(application, raise_server_exceptions=False):
                started = _wait_for(condition=warned)
                registration = application.state.all_sources_registration
        finally:
            _registries.clear()

        # Startup finished with HyperDX down, and shutdown stopped the retry.
        assert (started, registration.cancelled()) == (True, True)
