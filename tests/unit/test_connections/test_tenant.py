#  Project:      dfe-engine
#  File:         tests/unit/test_connections/test_tenant.py
#  Purpose:      Tests for TenantScopedClient wrapping logic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for TenantScopedClient (custom-settings tenant injection).

Uses a simple stub class instead of unittest.mock to verify that
``DFE_current_tenant_id`` is injected (comma-joined, fail-closed) into the
settings kwarg correctly.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.connections.tenant import TenantScopedClient


class StubCHClient:
    """Minimal stub that records calls for verification.

    NOT a mock - a simple test double that captures arguments.
    """

    def __init__(self) -> None:
        self.last_call: dict[str, Any] = {}

    def query(self, sql: str, *args: Any, **kwargs: Any) -> str:
        self.last_call = {"method": "query", "sql": sql, "args": args, "kwargs": kwargs}
        return "query_result"

    def command(self, sql: str, *args: Any, **kwargs: Any) -> str:
        self.last_call = {"method": "command", "sql": sql, "args": args, "kwargs": kwargs}
        return "command_result"

    def query_df(self, sql: str, *args: Any, **kwargs: Any) -> str:
        self.last_call = {
            "method": "query_df",
            "sql": sql,
            "args": args,
            "kwargs": kwargs,
        }
        return "df_result"


_SETTING = "DFE_current_tenant_id"


class TestTenantScopedClient:
    """Test the DFE_current_tenant_id injection into query settings."""

    def test_query_injects_tenant(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-123"])

        result = client.query("SELECT 1")
        assert result == "query_result"
        assert stub.last_call["kwargs"]["settings"][_SETTING] == "org-123"

    def test_multiple_org_ids_comma_joined(self) -> None:
        # Multi-org principal is scoped to the comma-joined list (the policy's
        # splitByChar splits it), NOT just the first org.
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["acme", "globex"])

        client.query("SELECT 1")
        assert stub.last_call["kwargs"]["settings"][_SETTING] == "acme,globex"

    def test_empty_org_ids_injects_empty_fail_closed(self) -> None:
        # FAIL CLOSED: the setting is ALWAYS injected; empty org_ids -> '' (which
        # matches no _org_id -> 0 rows), NEVER omitted (omitting would leak a stale
        # value from the pooled reader session).
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=[])

        client.query("SELECT 1")
        assert stub.last_call["kwargs"]["settings"][_SETTING] == ""

    def test_command_injects_tenant(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-456"])

        result = client.command("INSERT INTO t VALUES (1)")
        assert result == "command_result"
        assert stub.last_call["kwargs"]["settings"][_SETTING] == "org-456"

    def test_query_df_injects_tenant(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-789"])

        result = client.query_df("SELECT * FROM t")
        assert result == "df_result"
        assert stub.last_call["kwargs"]["settings"][_SETTING] == "org-789"

    def test_preserves_existing_settings(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-abc"])

        client.query("SELECT 1", settings={"max_execution_time": 30})
        settings = stub.last_call["kwargs"]["settings"]
        assert settings[_SETTING] == "org-abc"
        assert settings["max_execution_time"] == 30

    def test_tenant_setting_is_authoritative(self) -> None:
        # A client cannot widen its own scope: our tenant value overwrites a
        # client-supplied DFE_current_tenant_id.
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-abc"])

        client.query("SELECT 1", settings={_SETTING: "other-org"})
        assert stub.last_call["kwargs"]["settings"][_SETTING] == "org-abc"

    def test_tenant_setting_value_property(self) -> None:
        assert TenantScopedClient(StubCHClient(), org_ids=["a", "b"]).tenant_setting_value == "a,b"
        assert TenantScopedClient(StubCHClient(), org_ids=[]).tenant_setting_value == ""

    def test_org_ids_property(self) -> None:
        client = TenantScopedClient(StubCHClient(), org_ids=["a", "b"])
        assert client.org_ids == ["a", "b"]

    def test_forwards_positional_args(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-x"])

        client.query("SELECT {val:UInt32}", "extra_arg")
        assert stub.last_call["args"] == ("extra_arg",)

    def test_forwards_extra_kwargs(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-x"])

        client.query("SELECT 1", parameters={"val": 42})
        assert stub.last_call["kwargs"]["parameters"] == {"val": 42}
        assert stub.last_call["kwargs"]["settings"][_SETTING] == "org-x"


class TestReadonlyMode:
    """readonly=1 fixed users reject changing any non-CHANGEABLE_IN_READONLY
    setting, so the wrapper drops caller settings (keeping only the tenant one)."""

    def test_readonly_strips_caller_settings_keeps_tenant(self) -> None:
        # dfe_tenant_reader: readonly + tenant_filtered. The sampler's
        # max_execution_time would be REJECTED under readonly=1, so it is dropped;
        # DFE_current_tenant_id (the one CHANGEABLE_IN_READONLY setting) survives.
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["acme"], readonly=True)

        client.query("SELECT 1", settings={"max_execution_time": 30})
        settings = stub.last_call["kwargs"]["settings"]
        assert settings == {_SETTING: "acme"}
        assert "max_execution_time" not in settings

    def test_analyst_ro_mode_no_tenant_setting(self) -> None:
        # dfe_analyst_ro: readonly, NOT tenant_filtered (no row policy targets it,
        # and DFE_current_tenant_id is not changeable for it). Caller settings are
        # dropped and NO tenant setting is injected.
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=[], tenant_filtered=False, readonly=True)

        client.query("SELECT 1", settings={"max_execution_time": 30})
        assert stub.last_call["kwargs"]["settings"] == {}

    def test_analyst_ro_mode_command_and_df_drop_settings(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=[], tenant_filtered=False, readonly=True)

        client.command("OPTIMIZE TABLE t", settings={"max_execution_time": 5})
        assert stub.last_call["kwargs"]["settings"] == {}
        client.query_df("SELECT 1", settings={"max_execution_time": 5})
        assert stub.last_call["kwargs"]["settings"] == {}

    def test_readonly_property_reflects_flag(self) -> None:
        assert TenantScopedClient(StubCHClient(), org_ids=["a"], readonly=True).readonly is True
        assert TenantScopedClient(StubCHClient(), org_ids=["a"]).readonly is False
