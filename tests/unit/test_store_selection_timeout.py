#  Project:      dfe-engine
#  File:         tests/unit/test_store_selection_timeout.py
#  Purpose:      A document store that never answers fails a call within the selection timeout
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""How long a call to a document store that accepts connections and never answers waits.

The store here is a listening socket that never accepts, which is what a stalled or
partitioned store looks like to the client: the connection opens and nothing comes back.
A store that answers the handshake and then stalls is covered against a real store in
``tests/integration/test_docustore_socket_timeout.py``.
"""

import socket
import time
from collections.abc import Iterator

import pytest
from pymongo.errors import ServerSelectionTimeoutError

from dfe_engine.auth.store_backend import resolve_store_backend
from dfe_engine.settings import AccountStoreSettings, load_settings
from dfe_engine.store.documents import DocuStore

# Above the five-second default plus the client's own bookkeeping, far below pymongo's thirty.
_DEFAULT_CEILING_SECONDS = 10.0


@pytest.fixture
def silent_store() -> Iterator[str]:
    """A mongodb URI for a socket that takes connections and never answers one."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        host, port = listener.getsockname()
        yield f"mongodb://{host}:{port}/?directConnection=true"


def _seconds_to_fail(store: DocuStore) -> float:
    started = time.monotonic()
    with pytest.raises(ServerSelectionTimeoutError):
        store.ping()
    return time.monotonic() - started


def test_a_store_that_never_answers_fails_a_call_within_the_default(silent_store):
    store = DocuStore(silent_store, "dfe_engine_test")
    try:
        assert _seconds_to_fail(store) < _DEFAULT_CEILING_SECONDS
    finally:
        store.close()


def test_the_engine_builds_its_store_with_the_configured_timeout(silent_store):
    started = time.monotonic()

    backend = resolve_store_backend(
        "auto", silent_store, "dfe_engine_test", server_selection_timeout=0.3, socket_timeout=30.0
    )

    assert backend == ("yaml", None)
    assert time.monotonic() - started < 3.0


def test_the_timeout_defaults_to_five_seconds():
    assert AccountStoreSettings().server_selection_timeout_seconds == 5.0


def test_the_timeout_is_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("DFE_AUTH_ACCOUNTS_STORE_SERVER_SELECTION_TIMEOUT_SECONDS", "1.5")

    assert load_settings().auth.accounts_store.server_selection_timeout_seconds == 1.5


def test_a_timeout_of_zero_is_refused():
    with pytest.raises(ValueError, match="greater than 0"):
        AccountStoreSettings(server_selection_timeout_seconds=0)


def test_the_answer_timeout_defaults_to_thirty_seconds():
    assert AccountStoreSettings().socket_timeout_seconds == 30.0


def test_the_answer_timeout_is_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("DFE_AUTH_ACCOUNTS_STORE_SOCKET_TIMEOUT_SECONDS", "12.5")

    assert load_settings().auth.accounts_store.socket_timeout_seconds == 12.5


def test_an_answer_timeout_of_zero_is_refused():
    with pytest.raises(ValueError, match="greater than 0"):
        AccountStoreSettings(socket_timeout_seconds=0)
