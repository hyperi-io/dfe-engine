#  Project:      dfe-engine
#  File:         tests/support/pausable_relay.py
#  Purpose:      A loopback TCP relay to a backing service whose traffic a test can hold
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A loopback TCP relay in front of a real backing service, which a test can hold.

While held, every byte either side writes waits in the relay, so a client sees a
server that has stopped answering mid-conversation, as a stalled store looks. TLS
passes through untouched, so the client still verifies the server's certificate.
"""

import contextlib
import socket
import threading
from urllib.parse import urlsplit, urlunsplit

from tests.support.loopback import wake_accept

_READ_BYTES = 65536
_JOIN_SECONDS = 5.0
_CONNECT_SECONDS = 10.0


class PausableRelay:
    """Relays each connection made to :attr:`port` on loopback to *upstream*.

    Attributes:
        arrived: Set when a byte reaches the relay while it is held.
    """

    def __init__(self, upstream: tuple[str, int]) -> None:
        self._upstream = upstream
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self._flowing = threading.Event()
        self._flowing.set()
        self.arrived = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._sockets: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._listener.getsockname()[1]

    def relayed_uri(self, uri: str) -> str:
        """*uri* with its host and port replaced by this relay's, every other part kept.

        The host becomes ``localhost``, which the harness's gateway certificate names.
        """
        parts = urlsplit(uri)
        credentials, _, _ = parts.netloc.rpartition("@")
        netloc = f"{credentials}@localhost:{self.port}" if credentials else f"localhost:{self.port}"
        return urlunsplit(parts._replace(netloc=netloc))

    def hold(self) -> None:
        """Stop passing bytes in either direction until :meth:`release`."""
        self.arrived.clear()
        self._flowing.clear()

    def release(self) -> None:
        """Pass every held byte on, and every byte after it."""
        self._flowing.set()

    def close(self) -> None:
        """Stop accepting and close every relayed connection."""
        with self._lock:
            self._closed = True
            sockets = list(self._sockets)
        self.release()
        wake_accept(self._listener)
        self._listener.close()
        self._thread.join(timeout=_JOIN_SECONDS)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.close()

    def _accept(self) -> None:
        while True:
            try:
                client, _addr = self._listener.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self._upstream, timeout=_CONNECT_SECONDS)
            except OSError:
                client.close()
                continue
            upstream.settimeout(None)
            with self._lock:
                if self._closed:
                    client.close()
                    upstream.close()
                    return
                self._sockets.extend((client, upstream))
            for source, sink in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(source, sink), daemon=True).start()

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(_READ_BYTES):
                if not self._flowing.is_set():
                    self.arrived.set()
                    self._flowing.wait()
                sink.sendall(data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                sink.shutdown(socket.SHUT_WR)
