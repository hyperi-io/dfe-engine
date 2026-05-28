#  Project:      dfe-engine
#  File:         tests/unit/test_connections/test_tenant.py
#  Purpose:      Tests for TenantScopedClient wrapping logic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for TenantScopedClient.

Uses a simple stub class instead of unittest.mock to verify that
tenant_id is injected into the settings kwarg correctly.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.connections.tenant import TenantScopedClient


class StubCHClient:
    """Minimal stub that records calls for verification.

    NOT a mock — a simple test double that captures arguments.
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


class TestTenantScopedClient:
    """Test tenant_id injection into query settings."""

    def test_query_injects_tenant_id(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-123"])

        result = client.query("SELECT 1")
        assert result == "query_result"
        assert stub.last_call["kwargs"]["settings"]["current_tenant_id"] == "org-123"

    def test_command_injects_tenant_id(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-456"])

        result = client.command("INSERT INTO t VALUES (1)")
        assert result == "command_result"
        assert stub.last_call["kwargs"]["settings"]["current_tenant_id"] == "org-456"

    def test_query_df_injects_tenant_id(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-789"])

        result = client.query_df("SELECT * FROM t")
        assert result == "df_result"
        assert stub.last_call["kwargs"]["settings"]["current_tenant_id"] == "org-789"

    def test_no_org_ids_skips_injection(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=[])

        client.query("SELECT 1")
        assert "settings" not in stub.last_call["kwargs"]

    def test_preserves_existing_settings(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-abc"])

        client.query("SELECT 1", settings={"max_execution_time": 30})
        settings = stub.last_call["kwargs"]["settings"]
        assert settings["current_tenant_id"] == "org-abc"
        assert settings["max_execution_time"] == 30

    def test_multiple_org_ids_uses_first(self) -> None:
        stub = StubCHClient()
        client = TenantScopedClient(stub, org_ids=["org-first", "org-second"])

        client.query("SELECT 1")
        assert stub.last_call["kwargs"]["settings"]["current_tenant_id"] == "org-first"

    def test_tenant_id_property(self) -> None:
        client = TenantScopedClient(StubCHClient(), org_ids=["org-abc"])
        assert client.tenant_id == "org-abc"

    def test_tenant_id_none_when_empty(self) -> None:
        client = TenantScopedClient(StubCHClient(), org_ids=[])
        assert client.tenant_id is None

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
        assert stub.last_call["kwargs"]["settings"]["current_tenant_id"] == "org-x"
