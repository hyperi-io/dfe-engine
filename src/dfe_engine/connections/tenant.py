#  Project:      dfe-engine
#  File:         connections/tenant.py
#  Purpose:      Tenant-scoped ClickHouse client wrapper
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""TenantScopedClient wraps a clickhouse-connect client to inject tenant_id.

For customer-scoped roles, injects ``current_tenant_id`` as a custom
setting on every query.  ClickHouse row policies use
``getSetting('current_tenant_id')`` to filter rows.

For users with multiple org_ids, uses the first org_id.  The caller
can override this by passing a specific org_id.
"""

from __future__ import annotations

from typing import Any


class TenantScopedClient:
    """Wraps a clickhouse-connect client to inject tenant_id per query.

    Args:
        client: A clickhouse-connect client instance.
        org_ids: Organisation IDs for the authenticated user.
    """

    def __init__(self, client: Any, org_ids: list[str]) -> None:
        self._client = client
        self._org_ids = org_ids

    @property
    def org_ids(self) -> list[str]:
        """Return the org_ids this client is scoped to."""
        return self._org_ids

    @property
    def tenant_id(self) -> str | None:
        """Return the active tenant_id (first org_id), or None."""
        return self._org_ids[0] if self._org_ids else None

    def _inject_tenant_settings(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Inject current_tenant_id into the settings dict if org_ids present."""
        if self._org_ids:
            settings = dict(kwargs.pop("settings", None) or {})
            settings["current_tenant_id"] = self._org_ids[0]
            kwargs["settings"] = settings
        return kwargs

    def query(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        """Execute a query with tenant_id setting injected.

        Args:
            sql: SQL query string.
            *args: Positional arguments forwarded to the underlying client.
            **kwargs: Keyword arguments forwarded to the underlying client.
                The ``settings`` kwarg is augmented with ``current_tenant_id``.

        Returns:
            Query result from the underlying clickhouse-connect client.
        """
        kwargs = self._inject_tenant_settings(kwargs)
        return self._client.query(sql, *args, **kwargs)

    def command(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        """Execute a command with tenant_id setting injected.

        Args:
            sql: SQL command string.
            *args: Positional arguments forwarded to the underlying client.
            **kwargs: Keyword arguments forwarded to the underlying client.

        Returns:
            Command result from the underlying clickhouse-connect client.
        """
        kwargs = self._inject_tenant_settings(kwargs)
        return self._client.command(sql, *args, **kwargs)

    def query_df(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        """Execute a query returning a DataFrame, with tenant_id injected.

        Args:
            sql: SQL query string.
            *args: Positional arguments forwarded to the underlying client.
            **kwargs: Keyword arguments forwarded to the underlying client.

        Returns:
            DataFrame result from the underlying clickhouse-connect client.
        """
        kwargs = self._inject_tenant_settings(kwargs)
        return self._client.query_df(sql, *args, **kwargs)
