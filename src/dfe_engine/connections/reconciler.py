#  Project:      dfe-engine
#  File:         connections/reconciler.py
#  Purpose:      Reconcile ClickHouse users and row policies for RBAC
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Reconcile ClickHouse users and row policies.

Called at startup to ensure the static CH users and row policies
required by the custom settings pattern exist.  Does NOT create
per-org users — the custom settings pattern uses a fixed set of
users with ``getSetting('current_tenant_id')`` for tenant isolation.
"""

from __future__ import annotations

from typing import Any

from hyperi_pylib.logger import logger
from pydantic import BaseModel, Field

from dfe_engine.connections.config import ConnectionConfig


class ReconcileResult(BaseModel):
    """Result of a reconciliation run.

    Attributes:
        users_created: List of CH usernames that were created.
        policies_created: List of row policy descriptions that were created.
        errors: List of error messages encountered.
    """

    users_created: list[str] = Field(default_factory=list)
    policies_created: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class Reconciler:
    """Ensures ClickHouse users and row policies exist.

    Uses the admin connection to execute DDL.  Creates missing static
    users and row policies for tables that have an ``org_id`` column.

    Args:
        admin_client: A clickhouse-connect client with admin privileges.
        config: Connection configuration defining the expected users.
    """

    def __init__(self, admin_client: Any, config: ConnectionConfig) -> None:
        self._client = admin_client
        self._config = config

    def reconcile(self) -> ReconcileResult:
        """Check and create missing CH users and row policies.

        Returns:
            ReconcileResult with actions taken and any errors.
        """
        result = ReconcileResult()

        created_users = self._ensure_users_exist(result)
        result.users_created = created_users

        created_policies = self._ensure_row_policies_exist(result)
        result.policies_created = created_policies

        if result.users_created:
            logger.info(
                "Reconciled CH users",
                created=result.users_created,
            )
        if result.policies_created:
            logger.info(
                "Reconciled CH row policies",
                created=result.policies_created,
            )
        if result.errors:
            logger.warning(
                "Reconciliation completed with errors",
                error_count=len(result.errors),
            )

        return result

    def _ensure_users_exist(self, result: ReconcileResult) -> list[str]:
        """Create any CH users defined in connections but missing in CH.

        Args:
            result: Accumulates errors encountered during creation.

        Returns:
            List of usernames that were created.
        """
        created: list[str] = []

        # Collect unique usernames from connection definitions
        expected_users: dict[str, str] = {}
        for conn in self._config.connections.values():
            if conn.user and conn.user != "default":
                expected_users[conn.user] = conn.database

        if not expected_users:
            return created

        try:
            existing_result = self._client.query("SELECT name FROM system.users")
            existing_names = {row[0] for row in existing_result.result_rows}
        except Exception as exc:
            result.errors.append(f"Failed to query system.users: {exc}")
            return created

        for username, database in expected_users.items():
            if username not in existing_names:
                try:
                    self._client.command(
                        f"CREATE USER IF NOT EXISTS '{username}'"
                        f" IDENTIFIED WITH no_password"
                        f" DEFAULT DATABASE {database}"
                    )
                    created.append(username)
                except Exception as exc:
                    result.errors.append(f"Failed to create user '{username}': {exc}")

        return created

    def _ensure_row_policies_exist(self, result: ReconcileResult) -> list[str]:
        """Create row policies on tables with org_id for tenant isolation.

        Row policies use ``getSetting('current_tenant_id')`` to filter
        rows.  Applied to all non-default users defined in the config.

        Args:
            result: Accumulates errors encountered during creation.

        Returns:
            List of policy description strings that were created.
        """
        created: list[str] = []
        tenant_tables = self._discover_tenant_tables(result)

        if not tenant_tables:
            return created

        # Tenant-scoped users (non-default)
        tenant_users = [
            conn.user
            for conn in self._config.connections.values()
            if conn.user and conn.user != "default"
        ]
        if not tenant_users:
            return created

        for table in tenant_tables:
            for username in tenant_users:
                policy_name = f"tenant_isolation_{table.replace('.', '_')}_{username}"
                try:
                    self._client.command(
                        f"CREATE ROW POLICY IF NOT EXISTS {policy_name}"
                        f" ON {table}"
                        f" FOR SELECT"
                        f" USING org_id = getSetting('current_tenant_id')"
                        f" TO {username}"
                    )
                    created.append(f"{policy_name} on {table} for {username}")
                except Exception as exc:
                    result.errors.append(f"Failed to create row policy '{policy_name}': {exc}")

        return created

    def _discover_tenant_tables(self, result: ReconcileResult) -> list[str]:
        """Find tables that have an org_id column.

        Args:
            result: Accumulates errors encountered during discovery.

        Returns:
            List of fully qualified table names (database.table).
        """
        try:
            query_result = self._client.query(
                "SELECT database, table FROM system.columns"
                " WHERE name = 'org_id'"
                " AND database NOT IN ('system', 'information_schema', 'INFORMATION_SCHEMA')"
                " GROUP BY database, table"
            )
            return [f"{row[0]}.{row[1]}" for row in query_result.result_rows]
        except Exception as exc:
            result.errors.append(f"Failed to discover tenant tables: {exc}")
            return []
