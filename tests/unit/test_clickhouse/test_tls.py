#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_tls.py
#  Purpose:      Tests for the shared ClickHouse TLS resolver
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for ``resolve_clickhouse_tls`` -- the single TLS builder every
clickhouse-connect client site in the engine goes through.
"""

import logging

import pytest
import scalo.crypto as scalo_crypto

from dfe_engine.clickhouse.tls import ClickHouseCaCertUnreadable, resolve_clickhouse_tls


@pytest.fixture(autouse=True)
def _reset_scalo_warned_once(monkeypatch):
    """scalo's verify-off warning is deduped per process -- clear it so every
    test observes its own warning rather than one an earlier test already fired.
    """
    monkeypatch.setattr(scalo_crypto, "_warned_once", set())


class TestSecureOff:
    def test_no_kwargs_when_insecure(self):
        tls = resolve_clickhouse_tls(secure=False, verify=None, ca_cert=None)
        assert tls.connect_kwargs() == {}

    def test_insecure_ignores_an_unreadable_ca_cert(self, tmp_path):
        # No behaviour change when secure is off: an unreadable CA next to a
        # plain-HTTP dev setup is not an outage.
        missing = tmp_path / "does-not-exist.pem"
        tls = resolve_clickhouse_tls(secure=False, verify=None, ca_cert=str(missing))
        assert tls.connect_kwargs() == {}


class TestSecureOnVerifyDefault:
    def test_verify_defaults_on(self, monkeypatch):
        monkeypatch.delenv("SCALO_TLS_VERIFY", raising=False)
        tls = resolve_clickhouse_tls(secure=True, verify=None, ca_cert=None)
        assert tls.secure is True
        assert tls.verify is True
        assert tls.connect_kwargs() == {"secure": True, "verify": True}


class TestSecureOnWithCaCert:
    def test_readable_ca_cert_is_carried_through(self, tmp_path):
        ca = tmp_path / "internal-ca.pem"
        ca.write_text("-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n")
        tls = resolve_clickhouse_tls(secure=True, verify=None, ca_cert=str(ca))
        assert tls.ca_cert == str(ca)
        assert tls.connect_kwargs() == {"secure": True, "verify": True, "ca_cert": str(ca)}

    def test_unreadable_ca_cert_refuses(self, tmp_path):
        missing = tmp_path / "does-not-exist.pem"
        with pytest.raises(ClickHouseCaCertUnreadable, match="DFE_CLICKHOUSE_CA_CERT"):
            resolve_clickhouse_tls(secure=True, verify=None, ca_cert=str(missing))

    def test_unreadable_ca_cert_names_the_configured_path(self, tmp_path):
        missing = tmp_path / "does-not-exist.pem"
        with pytest.raises(ClickHouseCaCertUnreadable, match=str(missing)):
            resolve_clickhouse_tls(secure=True, verify=None, ca_cert=str(missing))


class TestVerifyOff:
    def test_verify_false_is_carried_through(self):
        tls = resolve_clickhouse_tls(secure=True, verify=False, ca_cert=None)
        assert tls.verify is False
        assert tls.connect_kwargs() == {"secure": True, "verify": False}

    def test_verify_false_logs_one_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="scalo.crypto"):
            resolve_clickhouse_tls(secure=True, verify=False, ca_cert=None)
        assert any("DISABLED" in record.getMessage() for record in caplog.records)
