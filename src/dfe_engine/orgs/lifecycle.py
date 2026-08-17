#  Project:      dfe-engine
#  File:         orgs/lifecycle.py
#  Purpose:      Orchestrates org lifecycle across the registry and HyperDX
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Org lifecycle manager.

Orchestrates org creation and deletion across the OrgRegistry and HyperDXClient.
HyperDX operations are non-fatal - failures are logged as warnings and the org
operation proceeds.

ClickHouse isolation is NO LONGER a per-org database (the retired 2.1 model);
under the shared-table + ``_org_id`` model it is a per-org ROLE + restrictive row
policy, reconciled from the Org registry by
``governance.ch.ChRbacReconciler`` - not here.

Usage::

    from pathlib import Path
    from dfe_engine.orgs.lifecycle import OrgLifecycleManager
    from dfe_engine.orgs.registry import OrgRegistry

    registry = OrgRegistry(Path("/etc/dfe/orgs"))
    manager = OrgLifecycleManager(registry)
    org = await manager.create_org("acme", org_ids=["acme"], admin_id="admin")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.auth.audit import (
    audit_org_change,
    audit_org_hyperdx_failed,
    audit_org_hyperdx_provisioned,
)
from dfe_engine.orgs.models import Org
from dfe_engine.orgs.registry import OrgRegistry

if TYPE_CHECKING:
    from dfe_engine.connections.config import ConnectionConfig
    from dfe_engine.hyperdx.client import HyperDXClient


class OrgLifecycleManager:
    """Orchestrates org lifecycle across OrgRegistry and HyperDXClient.

    HyperDX operations are non-fatal: failures are logged and do not prevent the
    org operation from completing. Per-org ClickHouse isolation (row policies on
    ``_org_id``) is applied by ``governance.ch.ChRbacReconciler`` from the
    registry, not here - orgs share tables.
    """

    def __init__(
        self,
        registry: OrgRegistry,
        hyperdx_client: HyperDXClient | None = None,
        connection_config: ConnectionConfig | None = None,
    ) -> None:
        self._registry = registry
        self._hdx = hyperdx_client
        self._conn_config = connection_config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_org(
        self,
        name: str,
        *,
        org_ids: list[str] | None = None,
        display_name: str = "",
        admin_id: str,
    ) -> Org:
        """Create a new org and link it to the fork's default HyperDX team.

        Creates the org in the registry and records the shared default team
        (non-fatal). Per-org ClickHouse row-policy isolation is reconciled from
        the registry by the CH RBAC reconciler, not here.

        Args:
            name: Unique org name.
            org_ids: Tenant IDs for ClickHouse row-level security.
            display_name: Human-readable label.
            admin_id: Identity of the admin performing the operation.

        Returns:
            The newly created Org.

        Raises:
            ValueError: If an org with this name already exists.
        """
        org = self._registry.create(name, org_ids=org_ids, display_name=display_name)
        org = await self._provision_hyperdx(org)
        audit_org_change(admin_id=admin_id, org_name=name, change="created")
        return org

    async def delete_org(self, name: str, *, admin_id: str) -> None:
        """Delete an org from the registry.

        The dfe-hyperdx fork exposes no team-deletion endpoint, so the shared
        default team is untouched. The CH RBAC reconciler drops the org's role
        + row policies on its next run.

        Args:
            name: Org to delete.
            admin_id: Identity of the admin performing the operation.

        Raises:
            KeyError: If no org with *name* exists.
        """
        org = self._registry.get(name)
        if org is None:
            raise KeyError(name)

        self._registry.delete(name)
        audit_org_change(admin_id=admin_id, org_name=name, change="deleted")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _provision_hyperdx(self, org: Org) -> Org:
        """Confirm the fork's default team and persist its ID on the org.

        The fork exposes no per-org team creation; every org shares the
        deployment's default team (JIT-created on first contact) until the
        per-team credential seam (dfe-engine#124) lands. Non-fatal: if HyperDX
        is unavailable, the org record is unchanged.

        Args:
            org: The org to provision.

        Returns:
            Updated Org with hyperdx_team_id set (unchanged on failure).
        """
        if self._hdx is None:
            return org

        try:
            team = await self._hdx.get_team()
        except Exception as exc:
            audit_org_hyperdx_failed(org_name=org.name, error=str(exc))
            return org

        team_id = str(team.get("_id", "")) if team else ""
        if team_id:
            audit_org_hyperdx_provisioned(org_name=org.name, team_id=team_id)

            # Ensure the tenant_reader ClickHouse connection exists on the team
            # (idempotent - the fork does not dedupe connections by name).
            if self._conn_config is not None:
                conn = self._conn_config.connections.get("tenant_reader")
                if conn is not None:
                    import os

                    try:
                        await self._hdx.ensure_connection(
                            name="tenant_reader",
                            host=conn.host,
                            port=conn.port,
                            username=conn.user,
                            password=os.environ.get(conn.password_env, ""),
                        )
                    except Exception as exc:
                        logger.warning(
                            "HyperDX connection create failed",
                            org_name=org.name,
                            error=str(exc),
                        )

            return self._registry.update(org.name, hyperdx_team_id=team_id)

        audit_org_hyperdx_failed(org_name=org.name, error="get_team returned no team")
        return org
