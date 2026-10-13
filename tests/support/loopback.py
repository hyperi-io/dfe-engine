#  Project:      dfe-engine
#  File:         tests/support/loopback.py
#  Purpose:      Loopback listeners that tests count, and a server stop that cannot hang
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Loopback listeners for tests, and the one way to stop a ``serve_forever`` thread.

``CountingListener`` stands where a backing service would be and counts every
connection made to it, so a test can tell whether the code under test dialled at
all. ``stop_server`` ends a ``socketserver`` loop in bounded time and fails the test
when it cannot.
"""

import contextlib
import socket
import struct
import threading
from collections.abc import Callable
from socketserver import TCPServer
from typing import Literal

_JOIN_SECONDS = 5.0
_FIRST_BYTES_SECONDS = 2.0
_READ_BYTES = 65536

type EndOfConnection = Literal["eof", "reset"]
"""How a listener ends a connection once the client has written: a clean EOF, or a reset."""


def wake_accept(listener: socket.socket) -> None:
    """Wake a thread parked in ``accept()`` on ``listener``.

    Closing the socket alone leaves that thread parked, and a later child exit
    resumes it on the reused fd. Shutting the socket down first makes the
    ``accept()`` raise on platforms that allow ``shutdown`` on a listening
    socket. On macOS that call fails with ``ENOTCONN`` and does not wake
    ``accept``, so a short connect to the port is used instead: ``accept``
    returns, and the caller closes or finishes the request as usual.
    """
    try:
        listener.shutdown(socket.SHUT_RDWR)
        return
    except OSError:
        pass
    try:
        host, port = listener.getsockname()[:2]
    except OSError:
        return
    with contextlib.suppress(OSError):
        with socket.create_connection((host, port), timeout=1.0):
            pass


def _end_after_first_bytes(
    conn: socket.socket, how: EndOfConnection, record: Callable[[bytes], None]
) -> None:
    """Wait for the client's first write, end ``conn`` the way ``how`` says, then close it.

    ``"eof"`` shuts the write side and reads until the client hangs up, so nothing is
    left unread for the close to answer with a reset. ``"reset"`` closes with a zero
    linger, which sends a reset in place of a FIN. A client that never writes, or that
    has gone, ends the wait at ``_FIRST_BYTES_SECONDS``.

    ``record`` gets the first write before the connection ends, so a client that has
    seen the end can rely on it being recorded.
    """
    conn.settimeout(_FIRST_BYTES_SECONDS)
    try:
        with contextlib.suppress(OSError):
            if first := conn.recv(_READ_BYTES):
                record(first)
            if how == "reset":
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            else:
                conn.shutdown(socket.SHUT_WR)
                while conn.recv(_READ_BYTES):
                    pass
    finally:
        conn.close()


class CountingListener:
    """A loopback listener that accepts every connection and counts it.

    By default each connection is closed at once, so a client that dials fails fast.
    With ``hold=True`` each stays open and unanswered until ``close``, so a client
    waits on it until its own timeout.

    Closing at once races a client that writes first, as a TLS client does with its
    ClientHello. A close that finds the write already queued answers with a reset,
    and one that lands before it with a clean EOF. ``after_first_bytes`` removes the
    race: each connection waits for the client's first write, then ends as ``"eof"``
    or ``"reset"`` says, every time, and keeps that write in ``first_writes``.
    """

    def __init__(
        self, *, hold: bool = False, after_first_bytes: EndOfConnection | None = None
    ) -> None:
        self._hold = hold
        self._after_first_bytes = after_first_bytes
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._lock = threading.Lock()
        self._connections = 0
        self._first_writes: list[bytes] = []
        self._held: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._listener.getsockname()[1]

    @property
    def connections(self) -> int:
        """Connections accepted so far."""
        with self._lock:
            return self._connections

    @property
    def first_writes(self) -> list[bytes]:
        """The first write of each connection so far, when ``after_first_bytes`` is set."""
        with self._lock:
            return list(self._first_writes)

    def _record_first_write(self, data: bytes) -> None:
        with self._lock:
            self._first_writes.append(data)

    def _accept(self) -> None:
        while True:
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self._connections += 1
                if self._hold:
                    self._held.append(conn)
            if self._hold:
                continue
            if self._after_first_bytes is None:
                conn.close()
            else:
                _end_after_first_bytes(conn, self._after_first_bytes, self._record_first_write)

    def close(self) -> None:
        wake_accept(self._listener)
        self._listener.close()
        self._thread.join(timeout=_JOIN_SECONDS)
        with self._lock:
            for conn in self._held:
                conn.close()


def stop_server(
    server: TCPServer, thread: threading.Thread, *, timeout: float = _JOIN_SECONDS
) -> None:
    """Stop ``server``'s ``serve_forever`` ``thread`` within ``timeout`` seconds.

    ``server.shutdown()`` waits, with no bound, for the loop to notice the request.
    A loop parked in ``accept()`` never does: ``select`` reported a connection and
    another reader took it first. So ``shutdown()`` runs on its own thread and the
    listening socket is shut down to wake the ``accept()``.

    Raises:
        TimeoutError: The loop was still running after ``timeout`` seconds. The
            socket is left open, because closing it under a parked ``accept()`` is
            the fd-reuse hazard ``wake_accept`` exists to avoid.
    """
    threading.Thread(target=server.shutdown, daemon=True).start()
    wake_accept(server.socket)
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"serve_forever was still running {timeout}s after shutdown")
    server.server_close()
