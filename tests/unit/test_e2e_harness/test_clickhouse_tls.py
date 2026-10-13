#  Project:      dfe-engine
#  File:         tests/unit/test_e2e_harness/test_clickhouse_tls.py
#  Purpose:      Assert the live suites reach a TLS ClickHouse over TLS
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""DFE_E2E_CH_SECURE decides whether the live suites speak TLS to ClickHouse.

A loopback listener stands where ClickHouse would be and keeps the first bytes each
connection writes: a TLS client opens with a handshake record, a plain HTTP client
with its request line.
"""

import pytest
from clickhouse_connect.driver.exceptions import OperationalError

from tests.e2e.conftest import connect_clickhouse, load_config
from tests.support.loopback import CountingListener

# The content type byte that opens every TLS handshake record, ClientHello included.
_TLS_HANDSHAKE = 0x16


@pytest.fixture
def clickhouse(monkeypatch: pytest.MonkeyPatch):
    listener = CountingListener(after_first_bytes="eof")
    monkeypatch.setenv("DFE_E2E_CH_HOST", "127.0.0.1")
    monkeypatch.setenv("DFE_E2E_CH_PORT", str(listener.port))
    yield listener
    listener.close()


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_a_secure_clickhouse_is_reached_over_tls(
    clickhouse: CountingListener, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DFE_E2E_CH_SECURE", value)

    with pytest.raises(OperationalError):
        connect_clickhouse(load_config())

    assert clickhouse.first_writes, "the client never wrote to the listener"
    assert all(first[0] == _TLS_HANDSHAKE for first in clickhouse.first_writes), (
        clickhouse.first_writes
    )


@pytest.mark.parametrize("value", [None, "", "0", "false"])
def test_a_plain_clickhouse_is_reached_over_http(
    clickhouse: CountingListener, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("DFE_E2E_CH_SECURE", raising=False)
    else:
        monkeypatch.setenv("DFE_E2E_CH_SECURE", value)

    with pytest.raises(OperationalError):
        connect_clickhouse(load_config())

    assert clickhouse.first_writes, "the client never wrote to the listener"
    assert all(first.startswith((b"GET ", b"POST ")) for first in clickhouse.first_writes), (
        clickhouse.first_writes
    )
