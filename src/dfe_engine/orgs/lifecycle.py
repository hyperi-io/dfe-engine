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

from dfe_engine.auth.audit import (
    audit_org_change,
    audit_org_hyperdx_failed,
    audit_org_hyperdx_provisioned,
)
from dfe_engine.orgs.models import Org
from dfe_engine.orgs.registry import OrgRegistry

if TYPE_CHECKING:
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
    ) -> None:
        self._registry = registry
        self._hdx = hyperdx_client

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
        """Confirm the deployment's HyperDX team and persist its ID on the org.

        The engine authenticates as its own machine identity, so the team the fork
        returns is the deployment's DEFAULT team rather than one of the org's own -
        a user's team is resolved from their OIDC group, not from this record.

        A team's ClickHouse connection is the fork's to create, from the material
        ``GET /api/v1/hyperdx/connection`` serves against the caller's own token.
        The engine writing one here hands every team the same credential and stops
        the fork provisioning the per-org one at all (dfe-engine#124, #312).

        Non-fatal: if HyperDX is unavailable, the org record is unchanged.

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
            audit_org_hyperdx_failed(org_name=org.name, exc=exc)
            return org

        team_id = str(team.get("_id", "")) if team else ""
        if team_id:
            audit_org_hyperdx_provisioned(org_name=org.name, team_id=team_id)
            return self._registry.update(org.name, hyperdx_team_id=team_id)

        audit_org_hyperdx_failed(org_name=org.name)
        return org
