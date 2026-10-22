#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_orgs_off_loop.py
#  Purpose:      A stalled store holds up only the org request waiting on it, never the event loop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A stalled backend call holds up only the request waiting on it.

Each case stalls one call an org or ingest credential route makes to its backend, then
asks ``/livez`` for an answer while it is stalled. A route that made the call on the
event loop holds ``/livez`` up with it, as it would every request and the probes.
"""

from typing import Any

import pytest

from dfe_engine.api.deps import get_clickhouse_client
from tests.support.stalled_call import Setup, Stall, registry_call, request_while_stalled

ORGS = "/api/v1/orgs"
CREDENTIALS = f"{ORGS}/acme/ingest-credentials"


def _change_listener(app: Any, stall: Stall, _monkeypatch: pytest.MonkeyPatch) -> None:
    app.state.org_registry.on_change(stall.wait)


def _clickhouse(app: Any, stall: Stall, _monkeypatch: pytest.MonkeyPatch) -> None:
    class Stalled:
        def execute(self, sql: str, parameters: object = None, settings: object = None) -> list:
            stall.wait()
            return []

    app.dependency_overrides[get_clickhouse_client] = Stalled


# What stalls, and the request that waits on it.
_CASES: dict[str, tuple[Setup, str, str, dict | None]] = {
    "list orgs": (registry_call("list"), "GET", ORGS, None),
    "get an org": (registry_call("get"), "GET", f"{ORGS}/acme", None),
    "hold a create": (registry_call("reserved", hold=True), "POST", ORGS, {"name": "beta"}),
    "create an org": (registry_call("create"), "POST", ORGS, {"name": "beta"}),
    "a change listener": (_change_listener, "POST", ORGS, {"name": "beta"}),
    "hold a delete": (registry_call("deleting", hold=True), "DELETE", f"{ORGS}/acme", None),
    "delete an org": (registry_call("delete"), "DELETE", f"{ORGS}/acme", None),
    "read tenant ids": (_clickhouse, "GET", f"{ORGS}/available-ids", None),
    "apply a credential": (registry_call("get"), "PUT", f"{CREDENTIALS}/agents", {}),
    "delete a credential": (
        registry_call("get", credentials=True),
        "DELETE",
        f"{CREDENTIALS}/agents",
        None,
    ),
    "rotate a credential": (
        registry_call("get", credentials=True),
        "POST",
        f"{CREDENTIALS}/agents/rotate",
        None,
    ),
}


@pytest.fixture
def acme(client, admin_headers) -> None:
    """Org acme, owning ingest credential agents."""
    made = client.post(ORGS, json={"name": "acme"}, headers=admin_headers)
    assert made.status_code == 201, made.text
    minted = client.put(
        f"{CREDENTIALS}/agents", json={}, headers={**admin_headers, "If-None-Match": "*"}
    )
    assert minted.status_code == 201, minted.text


@pytest.mark.parametrize("case", list(_CASES))
def test_a_stalled_call_holds_up_only_its_own_request(
    case, client, app, admin_headers, acme, monkeypatch
):
    setup, method, path, body = _CASES[case]
    stall = Stall()
    setup(app, stall, monkeypatch)

    done = request_while_stalled(
        client, stall, case, method, path, json=body, headers=admin_headers
    )

    assert done.status_code < 500, done.text
