#  Project:      dfe-engine
#  File:         tests/support/uvicorn_access.py
#  Purpose:      Serve one request through a real uvicorn server and read its access lines
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A real uvicorn server for one request, and the access lines uvicorn writes for it.

``uvicorn.access`` is left as scalo's ``setup()`` leaves it: no handler of its own,
propagating to the root, where scalo's sink sits. uvicorn writes its access line only
while that logger has a handler to reach, so a test that hung one on it directly
would turn the line on.
"""

import asyncio
import logging
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager

import httpx
import uvicorn


class _Records(logging.Handler):
    """Every stdlib record from one logger that reaches the root, formatted."""

    def __init__(self, name: str) -> None:
        super().__init__(level=logging.DEBUG)
        self._name = name
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == self._name:
            self.lines.append(record.getMessage())


@contextmanager
def uvicorn_access_lines() -> Generator[list[str]]:
    """uvicorn's access lines written inside the block, as they reach the root logger."""
    access = logging.getLogger("uvicorn.access")
    saved = (list(access.handlers), access.propagate, access.level)
    root = logging.getLogger()
    records = _Records("uvicorn.access")
    access.handlers.clear()
    access.propagate = True
    root.addHandler(records)
    try:
        yield records.lines
    finally:
        root.removeHandler(records)
        access.handlers[:] = saved[0]
        access.propagate = saved[1]
        access.setLevel(saved[2])


def serve_once(config_for: Callable[[int], uvicorn.Config], path: str) -> int:
    """Serve one GET of *path* through a real uvicorn server, then stop it.

    Args:
        config_for: Builds the server's config for the port it listens on.
        path: The request path.

    Returns:
        The response status.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(config_for(port))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])))
    thread.start()
    try:
        deadline = time.monotonic() + 10.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("uvicorn did not start within 10s")
        return httpx.get(f"http://127.0.0.1:{port}{path}").status_code
    finally:
        server.should_exit = True
        thread.join(10.0)
        listener.close()
