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
import threading
from socketserver import TCPServer

_JOIN_SECONDS = 5.0


def wake_accept(listener: socket.socket) -> None:
    """Wake a thread parked in ``accept()`` on ``listener``.

    Closing the socket alone leaves that thread parked, and a later child exit
    resumes it on the reused fd. Shutting the socket down first makes the
    ``accept()`` raise. An ``OSError`` here (not connected, already closed) is
    ignored: there is nothing left to wake.
    """
    with contextlib.suppress(OSError):
        listener.shutdown(socket.SHUT_RDWR)


class CountingListener:
    """A loopback listener that accepts every connection and counts it.

    By default each connection is closed at once, so a client that dials fails fast.
    With ``hold=True`` each stays open and unanswered until ``close``, so a client
    waits on it until its own timeout.
    """

    def __init__(self, *, hold: bool = False) -> None:
        self._hold = hold
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self._lock = threading.Lock()
        self._connections = 0
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
            if not self._hold:
                conn.close()

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
