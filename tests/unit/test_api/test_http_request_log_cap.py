#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_http_request_log_cap.py
#  Purpose:      A request URL, and the user login in it, never reaches the engine's log at INFO
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine's logger writes no HTTP client's request line, so no URL's user login.

A directory lookup puts the user's login in the path (Okta's
``/users/<login>/groups``), and the HTTP clients write each request URL whole at
INFO. The child runs the engine's logger init, then sends that lookup through
scalo's client (httpx) and through httpx2, the transport Authlib's OIDC client
uses. A fresh interpreter, because the logger's sinks and the stdlib root handler
are process-wide, and a minimal environment, so nothing from the shell reaches it.
"""

import subprocess
import sys
from pathlib import Path

import dfe_engine

_LOGIN = "sentinel.user@example.com"
_OPAQUE_ID = "00u1sentinel2abc3"
_PROBE = "http-log-cap-probe"

_CHILD = f"""
import asyncio
import http.server
import threading

import httpx2
from scalo.http import AsyncHttpClient

from dfe_engine.api import init_scalo_logger

init_scalo_logger("dfe-test", otel_tracing=False)
from scalo.logger import logger


class Directory(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Directory)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{{server.server_address[1]}}/api/v1/users"


async def lookups():
    async with AsyncHttpClient() as client:
        await client.get(f"{{base}}/{_LOGIN}/groups?limit=200")
    async with httpx2.AsyncClient() as client:
        await client.get(f"{{base}}/{_LOGIN}/groups?limit=200")
        await client.get(f"{{base}}/{_OPAQUE_ID}/groups?limit=200")


asyncio.run(lookups())
logger.info("{_PROBE}")
logger.complete()
server.shutdown()
"""


def test_no_request_line_or_login_reaches_the_log_at_info(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(Path(dfe_engine.__file__).parents[1]),
            "OTEL_SDK_DISABLED": "true",
            "LOG_FORMAT": "json",
            "LOG_ENQUEUE": "0",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    output = proc.stdout + proc.stderr

    assert proc.returncode == 0, output
    # The sink writes INFO, so nothing below is missing for want of a live log.
    assert _PROBE in output
    assert "HTTP Request" not in output
    assert "example.com" not in output
    assert _OPAQUE_ID not in output
