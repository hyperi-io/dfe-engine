#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_manager_tls.py
#  Purpose:      ClickHouseManager builds its pool + client TLS args from settings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``ClickHouseManager`` is the one site that pools connections itself, so its
pool manager (the CA trust anchor) and its ``get_client`` kwargs must agree --
both go through the same :func:`resolve_clickhouse_tls`, patched at the
external-driver boundary rather than mocking engine code.
"""

from unittest.mock import patch

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.clickhouse.tls import ClickHouseCaCertUnreadable


@pytest.fixture(autouse=True)
def _reset_manager_singleton():
    ClickHouseManager.reset_instance()
    yield
    ClickHouseManager.reset_instance()


def test_insecure_config_passes_no_tls_kwargs():
    manager = ClickHouseManager({"ch_secure": False})
    with (
        patch("dfe_engine.clickhouse.clickhouse_manager.httputil.get_pool_manager") as pool,
        patch("clickhouse_connect.get_client") as get_client,
    ):
        manager._live_client()
    assert pool.call_args[1]["ca_cert"] is None
    kwargs = get_client.call_args[1]
    assert "secure" not in kwargs
    assert "verify" not in kwargs


def test_secure_config_wires_verify_and_ca_cert_into_the_pool(tmp_path):
    ca = tmp_path / "internal-ca.pem"
    ca.write_text("cert")
    manager = ClickHouseManager({"ch_secure": True, "ch_verify": True, "ch_ca_cert": str(ca)})
    with (
        patch("dfe_engine.clickhouse.clickhouse_manager.httputil.get_pool_manager") as pool,
        patch("clickhouse_connect.get_client") as get_client,
    ):
        manager._live_client()
    assert pool.call_args[1]["verify"] is True
    assert pool.call_args[1]["ca_cert"] == str(ca)
    kwargs = get_client.call_args[1]
    assert kwargs["secure"] is True
    assert kwargs["verify"] is True


def test_unreadable_ca_cert_refuses_at_connect(tmp_path):
    missing = tmp_path / "does-not-exist.pem"
    manager = ClickHouseManager({"ch_secure": True, "ch_ca_cert": str(missing)})
    with pytest.raises(ClickHouseCaCertUnreadable, match="DFE_CLICKHOUSE_CA_CERT"):
        manager._live_client()
