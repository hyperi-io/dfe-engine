#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_client_failure_log.py
#  Purpose:      A failed HyperDX call logs its class and status, never its text
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A failed HyperDX call logs the exception class, HTTP status and error code.

The HTTP client's exception text names the request URL, which is the fork's
in-cluster address. Each test makes a real call to a local stand-in and reads what
the engine's logger wrote.
"""

import json
import socket

from dfe_engine.hyperdx.client import HyperDXClient
from tests.support.refusing_api import refusing_api

FAILED = "HyperDX call failed (non-fatal)"


async def test_a_refused_call_logs_the_status_and_code_not_the_url(audit_events: list[dict]):
    refusal = {"error": "forbidden", "message": "backend detail the fork put in its answer"}
    with refusing_api(403, refusal) as server:
        team = await HyperDXClient(base_url=server.base_url, api_key="k").get_team()

    assert team is None
    (line,) = [e for e in audit_events if e["event"] == FAILED]
    assert (line["op"], line["error_type"], line["status"], line["code"]) == (
        "get_team",
        "HTTPStatusError",
        403,
        "forbidden",
    )
    assert "error" not in line
    logged = json.dumps(line)
    assert server.base_url not in logged
    assert "backend detail" not in logged


async def test_an_unreachable_fork_logs_why_and_not_its_address(audit_events: list[dict]):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]

    team = await HyperDXClient(base_url=f"http://127.0.0.1:{port}", api_key="k").get_team()

    assert team is None
    (line,) = [e for e in audit_events if e["event"] == FAILED]
    assert (line["op"], line["error_type"], line["transport_failure"]) == (
        "get_team",
        "ConnectError",
        "connection_refused",
    )
    assert "error" not in line
    assert "127.0.0.1" not in json.dumps(line)
