#  Project:      dfe-engine
#  File:         tests/unit/test_loopback_support.py
#  Purpose:      The loopback helpers count connections and stop a server in bounded time
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``CountingListener`` and ``stop_server`` against the states that used to hang a run.

A ``serve_forever`` loop parks in ``accept()`` when ``select`` reported a connection
and another reader took it first. ``BaseServer.shutdown()`` then waits for ever, so
the stop must wake the ``accept()`` itself, and must fail rather than wait when it
cannot.
"""

import socket
import threading
import time
from collections.abc import Callable
from typing import Any
from wsgiref.simple_server import WSGIServer, make_server

import pytest

from tests.support.loopback import CountingListener, stop_server

_WAIT = 10.0
_BOUND = 5.0


def _until(condition: Callable[[], bool], timeout: float = _WAIT) -> bool:
    """Poll ``condition`` until it holds or ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _app(environ: Any, start_response: Any) -> list[bytes]:
    start_response("200 OK", [])
    return [b""]


class _ParkedInAccept(WSGIServer):
    """A server whose loop takes the pending connection itself, then parks in accept().

    The ``accept()`` that follows has no connection to return, which is where
    ``serve_forever`` stands after another reader wins the race for one.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.entering_accept = threading.Event()
        self.taken: list[socket.socket] = []

    def get_request(self) -> Any:
        taken, _addr = self.socket.accept()
        self.taken.append(taken)
        self.entering_accept.set()
        return super().get_request()


class _BlockedElsewhere(WSGIServer):
    """A server whose loop blocks where no socket shutdown can reach it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_request(self) -> Any:
        self.entered.set()
        self.release.wait()
        raise OSError("released")


def _serve(server_class: type[WSGIServer]) -> tuple[Any, threading.Thread, socket.socket]:
    """A running server, and a client whose connection its loop has picked up."""
    server = make_server("127.0.0.1", 0, _app, server_class=server_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(("127.0.0.1", server.server_port), timeout=_WAIT)
    return server, thread, client


def test_a_server_parked_in_accept_is_stopped_within_the_bound() -> None:
    server, thread, client = _serve(_ParkedInAccept)
    try:
        assert server.entering_accept.wait(_WAIT), "the loop never reached accept"

        stop_server(server, thread, timeout=_BOUND)

        assert not thread.is_alive()
        assert server.socket.fileno() == -1
    finally:
        client.close()
        for taken in server.taken:
            taken.close()


def test_a_server_that_cannot_be_stopped_fails_the_teardown_instead_of_hanging() -> None:
    server, thread, client = _serve(_BlockedElsewhere)
    timeout = 0.5
    try:
        assert server.entered.wait(_WAIT), "the loop never reached get_request"

        started = time.monotonic()
        with pytest.raises(TimeoutError, match="still running"):
            stop_server(server, thread, timeout=timeout)

        assert time.monotonic() - started < timeout + _BOUND
        assert server.socket.fileno() != -1, "closed under a loop that is still running"
    finally:
        server.release.set()
        thread.join(_WAIT)
        client.close()
    assert not thread.is_alive()
    server.server_close()


def test_a_counting_listener_counts_each_connection_and_closes_it_at_once() -> None:
    listener = CountingListener()
    try:
        for _ in range(3):
            with socket.create_connection(("127.0.0.1", listener.port), timeout=_WAIT) as client:
                assert client.recv(1) == b"", "the listener should have closed its end"
        assert _until(lambda: listener.connections == 3)
    finally:
        listener.close()


def test_a_holding_listener_keeps_each_connection_open_until_it_is_closed() -> None:
    listener = CountingListener(hold=True)
    client = socket.create_connection(("127.0.0.1", listener.port), timeout=_WAIT)
    try:
        assert _until(lambda: listener.connections == 1)
        client.setblocking(False)
        with pytest.raises(BlockingIOError):
            client.recv(1)
    finally:
        listener.close()
    client.settimeout(_WAIT)
    assert client.recv(1) == b""
    client.close()


def test_closing_a_listener_does_not_wait_out_its_accept_thread() -> None:
    """Without the shutdown the parked accept() outlives close() and the join runs out."""
    listener = CountingListener()

    started = time.monotonic()
    listener.close()

    assert time.monotonic() - started < _BOUND / 2
