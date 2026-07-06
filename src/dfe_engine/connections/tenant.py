#  Project:      dfe-engine
#  File:         connections/tenant.py
#  Purpose:      Tenant-scoped ClickHouse client wrapper (custom-settings model)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""TenantScopedClient wraps a fixed-user clickhouse-connect client for a read.

The custom-settings tenant model: a tenant-scoped principal (the ``org_analyst``
role, resolved to the fixed ``dfe_tenant_reader`` CH user by the ConnectionRegistry)
runs every query with the ``SQL_current_tenant_id`` custom setting, which the ONE
per-table RESTRICTIVE row policy reads via
``has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id)``. The
tenant-filtered variant of this wrapper is used for that principal; admin /
analyst (read-write) principals run as their own fixed user WITHOUT any wrapper
(targeted by no policy, so they see all rows).

The value is the caller's resolved ``org_ids`` (``AuthContext.org_ids``) comma
joined, so a MULTI-org principal is scoped to exactly their orgs. FAIL CLOSED: the
setting is ALWAYS injected - even for a principal with NO org_ids, where it is set
to the empty string, which the policy predicate resolves to zero rows. Never OMIT
the setting: the reader user defaults it to '' but omitting it on the shared reader
connection would leave a stale value from a prior query in the pooled session and
leak another tenant's rows.

Two orthogonal knobs let the ONE wrapper also serve the read-only ``dfe_analyst_ro``
fixed user (data_analyst_ro / data_viewer / infra_ro):

  * ``tenant_filtered`` (default True) - inject ``SQL_current_tenant_id``. Set
    False for ``dfe_analyst_ro``, which is NOT a row-policy target and whose
    profile does NOT make the setting CHANGEABLE_IN_READONLY (so injecting it
    would be rejected).
  * ``readonly`` (default False) - the wrapped fixed user runs CH ``readonly=1``.
    Both ``dfe_tenant_reader`` and ``dfe_analyst_ro`` do. CH then REJECTS any
    per-query attempt to CHANGE a setting other than CHANGEABLE_IN_READONLY ones,
    so caller-supplied settings (e.g. the sampler's ``max_execution_time``) are
    dropped before the query - the fixed user's profile already bounds it. The
    tenant setting we inject is the sole CHANGEABLE_IN_READONLY one and survives.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.governance.ch.models import TENANT_SETTING


class TenantScopedClient:
    """Wraps a fixed-user clickhouse-connect client for a scoped direct-CH read.

    Args:
        client: A clickhouse-connect client authenticated as one of the fixed
            read users (``dfe_tenant_reader`` when ``tenant_filtered``, else
            ``dfe_analyst_ro``).
        org_ids: The authenticated principal's resolved org IDs
            (``AuthContext.org_ids``). Empty -> the tenant setting is injected as
            '' (fail closed: zero rows), never omitted. Ignored when
            ``tenant_filtered`` is False.
        tenant_filtered: Inject ``SQL_current_tenant_id`` (the row-policy target,
            ``dfe_tenant_reader``). False for ``dfe_analyst_ro`` (no policy, no
            changeable tenant setting).
        readonly: The wrapped fixed user runs CH ``readonly=1``; drop
            caller-supplied per-query settings CH would reject (keeping only the
            injected tenant setting).
    """

    def __init__(
        self,
        client: Any,
        org_ids: list[str],
        *,
        tenant_filtered: bool = True,
        readonly: bool = False,
    ) -> None:
        self._client = client
        self._org_ids = org_ids
        self._tenant_filtered = tenant_filtered
        self._readonly = readonly

    @property
    def org_ids(self) -> list[str]:
        """Return the org_ids this client is scoped to."""
        return self._org_ids

    @property
    def readonly(self) -> bool:
        """True when the wrapped fixed user runs CH ``readonly=1``.

        The sampler's gated (logreducer) path talks to the UNWRAPPED native client
        (see ``clickhouse_reader._raw_client``) and so bypasses this wrapper's
        setting handling; it consults this flag to drop the per-query settings a
        readonly user would reject.
        """
        return self._readonly

    @property
    def tenant_setting_value(self) -> str:
        """The exact ``SQL_current_tenant_id`` value injected per query.

        Comma-joined org_ids (multi-org scoping), or '' when the principal has no
        orgs (fail closed). This is the string the row policy's ``splitByChar``
        splits, so an empty value matches no real ``_org_id``.
        """
        return ",".join(self._org_ids)

    def _inject_tenant_settings(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Rewrite the per-query ``settings`` for this fixed read user.

        Readonly first: a CH ``readonly=1`` user REJECTS any per-query attempt to
        CHANGE a setting other than the CHANGEABLE_IN_READONLY ones, so when
        ``readonly`` the caller-supplied settings (e.g. the sampler's
        ``max_execution_time``) are DROPPED - the fixed user's profile already
        bounds the query.

        Tenant next: when ``tenant_filtered`` the ``SQL_current_tenant_id`` setting
        is injected unconditionally (fail closed - comma-joined org_ids, or '' when
        empty, never skipped) and is authoritative, overwriting any client-supplied
        value so a query can never widen its own tenant scope. It is the sole
        CHANGEABLE_IN_READONLY setting for the tenant reader, so it survives the
        readonly drop above.
        """
        settings = dict(kwargs.pop("settings", None) or {})
        if self._readonly:
            settings = {}
        if self._tenant_filtered:
            settings[TENANT_SETTING] = self.tenant_setting_value
        kwargs["settings"] = settings
        return kwargs

    def query(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        """Execute a query with the tenant setting injected.

        Args:
            sql: SQL query string.
            *args: Positional arguments forwarded to the underlying client.
            **kwargs: Keyword arguments forwarded to the underlying client. The
                ``settings`` kwarg is augmented with ``SQL_current_tenant_id``.

        Returns:
            Query result from the underlying clickhouse-connect client.
        """
        kwargs = self._inject_tenant_settings(kwargs)
        return self._client.query(sql, *args, **kwargs)

    def command(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        """Execute a command with the tenant setting injected.

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
        """Execute a query returning a DataFrame, with the tenant setting injected.

        Args:
            sql: SQL query string.
            *args: Positional arguments forwarded to the underlying client.
            **kwargs: Keyword arguments forwarded to the underlying client.

        Returns:
            DataFrame result from the underlying clickhouse-connect client.
        """
        kwargs = self._inject_tenant_settings(kwargs)
        return self._client.query_df(sql, *args, **kwargs)
