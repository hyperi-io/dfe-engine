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

ClickHouse isolation is NO LONGER a per-org database (the retired 2.1 model) nor a
per-org role/per-group user (the retired interim model); under the shared-table +
``_org_id`` model it is ONE fixed row-filtered reader (``dfe_tenant_reader``) scoped
per query by the ``DFE_current_tenant_id`` custom setting, reconciled by
``governance.ch.ChRbacReconciler`` - not here. This manager only bakes each org's
tenant setting into that org's HyperDX connection.

Usage::

    from pathlib import Path
    from dfe_engine.orgs.lifecycle import OrgLifecycleManager
    from dfe_engine.orgs.registry import OrgRegistry

    registry = OrgRegistry(Path("/etc/dfe/orgs"))
    manager = OrgLifecycleManager(registry)
    org = await manager.create_org("acme", org_ids=["acme"], admin_id="admin")
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from scalo.logger import logger

from dfe_engine.auth.audit import (
    audit_org_change,
    audit_org_hyperdx_failed,
    audit_org_hyperdx_provisioned,
)
from dfe_engine.governance.ch import TENANT_READER_USER, TENANT_SETTING
from dfe_engine.orgs.models import Org
from dfe_engine.orgs.registry import OrgRegistry

if TYPE_CHECKING:
    from dfe_engine.connections.config import ConnectionConfig
    from dfe_engine.hyperdx.client import HyperDXClient


def _team_api_key_env(team_name: str) -> str:
    """Env var name that holds a HyperDX team's API key (derived from the team name)."""
    return f"HYPERDX_TEAM_API_KEY_{team_name.upper().replace('-', '_')}"


class OrgLifecycleManager:
    """Orchestrates org lifecycle across OrgRegistry and HyperDXClient.

    HyperDX operations are non-fatal: failures are logged and do not prevent the
    org operation from completing. ClickHouse isolation (the ONE ``_org_id`` row
    policy driven by ``DFE_current_tenant_id``) is applied by
    ``governance.ch.ChRbacReconciler`` from the registry, not here - orgs share
    tables and the shared ``dfe_tenant_reader`` user.
    """

    def __init__(
        self,
        registry: OrgRegistry,
        hyperdx_client: HyperDXClient | None = None,
        connection_config: ConnectionConfig | None = None,
        secrets_store: Any = None,
        *,
        per_group: bool = False,
        ga_team_name: str = "dfe",
    ) -> None:
        self._registry = registry
        self._hdx = hyperdx_client
        self._conn_config = connection_config
        # scalo.secrets seam (DfeSecrets): source of the shared dfe_tenant_reader
        # plaintext the HyperDX connection authenticates with. None -> no password
        # sourced (connection minted with an empty one, non-fatal).
        self._secrets = secrets_store
        # DFE_HYPERDX_PER_GROUP posture (Task B). False (GA default) = ONE shared
        # HyperDX team (`ga_team_name`) for every org; True (post-GA) = the richer
        # per-org team `customer-<org>`. Either way there is ONE connection per org
        # carrying its DFE_current_tenant_id setting, so tenant isolation is the
        # per-connection setting, never the team boundary.
        self._per_group = per_group
        self._ga_team_name = ga_team_name

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
        """Create a new org and provision its HyperDX team.

        Creates the org in the registry and creates a HyperDX team (non-fatal).
        Per-org ClickHouse row-policy isolation is reconciled from the registry
        by the CH RBAC reconciler, not here.

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
        """Delete an org and its HyperDX team.

        Deletes the HyperDX team (if any) before removing the org from the
        registry. HyperDX failures are non-fatal. The CH RBAC reconciler drops
        the org's role + row policies on its next run.

        Args:
            name: Org to delete.
            admin_id: Identity of the admin performing the operation.

        Raises:
            KeyError: If no org with *name* exists.
        """
        org = self._registry.get(name)
        if org is None:
            raise KeyError(name)

        if org.hyperdx_team_id and self._hdx is not None:
            await self._hdx.delete_team(org.hyperdx_team_id)

        self._registry.delete(name)
        audit_org_change(admin_id=admin_id, org_name=name, change="deleted")

    async def revoke_member(self, org_name: str, email: str) -> bool:
        """Revoke a user's HyperDX team membership for an org.

        The propagation seam for account + group mutations that today never reach
        HyperDX: an account being disabled/deleted, or a user removed from an
        org's group (or the group's org access changing). When a user loses
        access to *org_name*, remove them from that org's HyperDX team
        (``customer-<org>``) so the org's HyperDX stops admitting them. Resolves
        the org's stored team API key (the ``hyperdx_team_api_key_env`` env var
        ``_provision_hyperdx`` writes) and issues the revoke. Non-fatal; returns
        True only when the revoke was issued.

        The account/group STORES + their routers live outside this module, so the
        one-line hook the sibling/handback must add (per affected org) is::

            await org_lifecycle.revoke_member(org_name, email)

        - group remove-member router (api/v1/account_groups.py) + the CLI
          (cli.groups_remove_member) + membership.sync helpers, and
        - account disable/delete router (enabled=False / delete).

        Args:
            org_name: Org whose HyperDX team the user is being removed from.
            email: The member's email (HyperDX invites/removes are by email).

        Returns:
            True if the revoke request was issued, False otherwise.
        """
        if self._hdx is None:
            return False
        org = self._registry.get(org_name)
        if org is None or not org.hyperdx_team_api_key_env:
            return False
        team_api_key = os.environ.get(org.hyperdx_team_api_key_env, "")
        if not team_api_key:
            return False
        return await self._hdx.remove_member(team_api_key, email)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fixed_reader_secret(self) -> str:
        """Read the shared dfe_tenant_reader plaintext from the scalo.secrets seam.

        ChRbacReconciler mints the fixed users' plaintext at ``ch/fixed/<name>``
        (mint-or-reuse); HyperDX needs the ``dfe_tenant_reader`` plaintext to connect
        as that shared, row-filtered reader. Missing store or secret -> empty string
        (non-fatal: the connection is corrected once the reconciler has minted the
        secret and the next publish/provision runs).
        """
        if self._secrets is None:
            return ""
        try:
            return self._secrets.get(f"ch/fixed/{TENANT_READER_USER}")
        except Exception:
            return ""

    def _existing_shared_team(self) -> tuple[str, str] | None:
        """The GA shared team (team_id, api_key_env) if a prior org already made it.

        GA posture (per_group False) puts every org on ONE HyperDX team, so the
        second org onwards must REUSE the first org's team rather than mint a
        duplicate ``dfe`` team. Returns the first stored (team_id, api_key_env) pair
        found in the registry, or None when no org has provisioned a team yet.
        """
        for existing in self._registry.list():
            if existing.hyperdx_team_id:
                return existing.hyperdx_team_id, existing.hyperdx_team_api_key_env
        return None

    async def _attach_tenant_connection(self, team_id: str, org: Org) -> None:
        """Attach the org's ClickHouse connection to its HyperDX team (tenant-reader model).

        The connection authenticates as the SHARED ``dfe_tenant_reader`` (secret
        ``ch/fixed/dfe_tenant_reader``) and carries the org's ``DFE_current_tenant_id``
        = comma-joined ``org_ids`` as a per-connection ClickHouse setting; the ONE
        restrictive row policy on each ``_org_id`` table scopes that reader to those
        orgs (empty setting -> 0 rows, fail closed). Network coordinates come from the
        ``default`` connection - every org hits the SAME ClickHouse as the SAME user,
        only the tenant setting differs. Non-fatal.
        """
        if self._conn_config is None:
            return
        base = self._conn_config.connections.get("default")
        if base is None:
            return
        try:
            await self._hdx.create_connection(
                team_id=team_id,
                name=org.name,
                host=base.host,
                port=base.port,
                database=base.database,
                user=TENANT_READER_USER,
                password=self._fixed_reader_secret(),
                settings={TENANT_SETTING: ",".join(org.org_ids or [])},
            )
        except Exception as exc:
            logger.warning(
                "HyperDX connection create failed",
                org_name=org.name,
                error=str(exc),
            )

    async def _provision_hyperdx(self, org: Org) -> Org:
        """Provision the org's HyperDX team + tenant connection, persist the team ID.

        The team MODEL is gated on DFE_HYPERDX_PER_GROUP (Task B): GA reuses ONE
        shared team across all orgs; per-group gives each org its own
        ``customer-<org>`` team. Either posture attaches ONE per-org connection that
        authenticates as ``dfe_tenant_reader`` and carries the org's tenant setting
        (Task A) - isolation is that setting, not the team. Non-fatal: if HyperDX is
        unavailable the org record is unchanged.

        Args:
            org: The org to provision.

        Returns:
            Updated Org with hyperdx_team_id set (unchanged on failure).
        """
        if self._hdx is None:
            return org

        # GA posture: reuse the one shared team if a prior org already minted it,
        # so N orgs never create N duplicate `dfe` teams.
        if not self._per_group:
            shared = self._existing_shared_team()
            if shared is not None:
                team_id, api_key_env = shared
                await self._attach_tenant_connection(team_id, org)
                audit_org_hyperdx_provisioned(org_name=org.name, team_id=team_id)
                return self._registry.update(
                    org.name,
                    hyperdx_team_id=team_id,
                    hyperdx_team_api_key_env=api_key_env,
                )

        team_name = f"customer-{org.name}" if self._per_group else self._ga_team_name
        try:
            team_id = await self._hdx.create_team(team_name)
        except Exception as exc:
            audit_org_hyperdx_failed(org_name=org.name, error=str(exc))
            return org

        if not team_id:
            audit_org_hyperdx_failed(org_name=org.name, error="create_team returned None")
            return org

        audit_org_hyperdx_provisioned(org_name=org.name, team_id=team_id)
        update_kwargs: dict[str, object] = {"hyperdx_team_id": team_id}

        await self._attach_tenant_connection(team_id, org)

        # Retrieve the team's own API key so we can invite members later. The env
        # var name derives from the team name (shared under GA, per-org otherwise).
        team_api_key = await self._hdx.get_team_api_key(team_id)
        if team_api_key:
            env_var = _team_api_key_env(team_name)
            os.environ[env_var] = team_api_key
            update_kwargs["hyperdx_team_api_key_env"] = env_var
            logger.info(
                "HyperDX team API key stored",
                org_name=org.name,
                env_var=env_var,
            )

        return self._registry.update(org.name, **update_kwargs)
