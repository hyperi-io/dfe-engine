#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_manager_config.py
#  Purpose:      ClickHouseManager singleton config binding (no live CH)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The get_instance() singleton is first-call-wins: every seam that can be the
first caller must pass the settings-derived config, or the whole process binds
to the hardcoded localhost defaults. clickhouse_connect.get_client is faked to
capture the kwargs - no live ClickHouse."""

from __future__ import annotations

import clickhouse_connect
import pytest

import dfe_engine.clickhouse.clickhouse_manager as ch_module
import dfe_engine.settings as settings_module
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.settings import ClickHouseSettings, DFESettings


class _FakeResult:
    def __init__(self, columns, rows):
        self.column_names = columns
        self.result_rows = rows


class _FakeConnectClient:
    def query(self, sql, parameters=None, settings=None, **_kw):
        return _FakeResult(["n"], [(2,)])

    def command(self, sql, *_a, **_kw):
        return None

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_singleton():
    ClickHouseManager.reset_instance()
    yield
    ClickHouseManager.reset_instance()


@pytest.fixture
def captured_client(monkeypatch) -> dict:
    """Fake clickhouse_connect.get_client, capturing its kwargs."""
    captured: dict = {}

    def fake_get_client(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return _FakeConnectClient()

    monkeypatch.setattr(clickhouse_connect, "get_client", fake_get_client)
    return captured


def test_active_hunt_leases_binds_settings_config(monkeypatch, captured_client):
    fake_settings = DFESettings(
        clickhouse=ClickHouseSettings(
            host="ch.hunts", port=8123, username="svc", password="pw", secure=False
        )
    )
    monkeypatch.setattr(settings_module, "_settings", fake_settings)

    from dfe_engine.api.v1.hunts import _active_hunt_leases

    leases = _active_hunt_leases(None)

    # 2 from the fake result row proves the real query path ran (the helper
    # swallows every exception into 0, so 0 would hide a broken seam).
    assert leases == 2
    assert captured_client["host"] == "ch.hunts"
    assert captured_client["port"] == 8123
    assert captured_client["username"] == "svc"


def test_get_adapter_passes_config_through_to_manager():
    from dfe_engine.query.datasources import get_adapter

    cfg = {"ch_host": "ch.adapter", "ch_port": 9999, "ch_secure": False}
    adapter = get_adapter("clickhouse:default", config=cfg)
    assert adapter.manager.target_config_data == cfg


def test_rule_creation_service_binds_service_config(captured_client):
    from dfe_engine.hunts.rule_creation_service import (
        RuleCreateRequest,
        RuleCreationService,
    )

    cfg = {"ch_host": "ch.rules", "ch_port": 8124, "ch_secure": False}
    service = RuleCreationService(ch_config=cfg)
    result = service.create_rule(
        RuleCreateRequest(
            name="test rule",
            user_sql="SELECT count() FROM db.tbl WHERE a = 1",
            estimate_cost=True,
        ),
        rule_id="r1",
    )
    assert result.cost_estimate is not None
    assert captured_client["host"] == "ch.rules"
    assert captured_client["port"] == 8124


def test_database_passed_to_admin_client(captured_client):
    """DFE_CLICKHOUSE_DATABASE (ch_database) must reach the admin client too,
    not just the restricted view client. Without it the export was a no-op."""
    ClickHouseManager.get_instance(
        {"ch_host": "ch.db", "ch_port": 8123, "ch_secure": False, "ch_database": "dfe_data"}
    ).get_clickhouse_client()
    assert captured_client["database"] == "dfe_data"


def test_no_database_key_when_unset(captured_client):
    ClickHouseManager.get_instance(
        {"ch_host": "ch.db", "ch_port": 8123, "ch_secure": False}
    ).get_clickhouse_client()
    assert "database" not in captured_client


def test_get_instance_warns_when_config_differs(monkeypatch):
    warnings: list[tuple] = []

    class _StubLogger:
        def warning(self, msg, **kw):
            warnings.append((msg, kw))

        def info(self, *a, **kw):
            pass

        def error(self, *a, **kw):
            pass

    monkeypatch.setattr(ch_module, "logger", _StubLogger())

    first = ClickHouseManager.get_instance({"ch_host": "a", "ch_port": 8123})
    assert ClickHouseManager.get_instance({"ch_host": "a", "ch_port": 8123}) is first
    assert warnings == []  # identical config: no noise

    other = ClickHouseManager.get_instance({"ch_host": "b", "ch_port": 9440})
    assert other is first  # still first-call-wins ...
    assert len(warnings) == 1  # ... but no longer silently
    assert warnings[0][1]["bound_host"] == "a"
    assert warnings[0][1]["requested_host"] == "b"
