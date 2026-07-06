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
per query by the ``SQL_current_tenant_id`` custom setting, reconciled by
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


def _team_api_key_secret_path(team_name: str) -> str:
    """scalo.secrets path that holds a HyperDX team's API key (derived from the team name)."""
    return f"hyperdx/team-api-key/{team_name}"


class OrgLifecycleManager:
    """Orchestrates org lifecycle across OrgRegistry and HyperDXClient.

    HyperDX operations are non-fatal: failures are logged and do not prevent the
    org operation from completing. ClickHouse isolation (the ONE ``_org_id`` row
    policy driven by ``SQL_current_tenant_id``) is applied by
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
        # plaintext the HyperDX connection authenticates with, AND the durable store
        # for each HyperDX team's minted API key (never the process environment -
        # that is lost on restart and invisible to sibling pods in a multi-replica
        # deploy). None -> no password sourced / no team key persisted; both
        # non-fatal.
        self._secrets = secrets_store
        # DFE_HYPERDX_PER_GROUP posture (Task B). False (GA default) = ONE shared
        # HyperDX team (`ga_team_name`) for every org; True (post-GA) = the richer
        # per-org team `customer-<org>`. Either way there is ONE connection per org
        # carrying its SQL_current_tenant_id setting, so tenant isolation is the
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
        """Delete an org, its per-org HyperDX connection, and (only if it is the
        last one on the team) the HyperDX team.

        POSTURE-AWARE (P1.4/P1.5): under the GA posture every org shares ONE
        HyperDX team, so an unconditional ``delete_team`` would destroy the team
        (and connections + members) that all the REMAINING orgs still live on.
        The team is deleted ONLY when no other org references the same
        ``hyperdx_team_id`` - i.e. this is the last org on it (always true under
        the per-group posture, where each org has its own team). The org's OWN
        per-org connection is always dropped so the shared team is not left with a
        dangling connection for a deleted tenant. HyperDX failures are non-fatal.
        The CH RBAC reconciler drops the org's role + row policies on its next run.

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
            others_on_team = any(
                other.name != name and other.hyperdx_team_id == org.hyperdx_team_id
                for other in self._registry.list()
            )
            # Always drop this org's own connection (harmless if it never had one).
            if org.hyperdx_connection_id:
                await self._hdx.delete_connection(org.hyperdx_team_id, org.hyperdx_connection_id)
            # Delete the team ONLY when no other org still lives on it, so a shared
            # (GA) team survives while any org references it.
            if not others_on_team:
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
        the org's stored team API key (the scalo.secrets path ``_provision_hyperdx``
        writes to ``hyperdx_team_api_key_path``) and issues the revoke. Non-fatal;
        returns True only when the revoke was issued.

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
        if org is None or not org.hyperdx_team_api_key_path:
            return False
        team_api_key = self._get_team_api_key(org.hyperdx_team_api_key_path)
        if not team_api_key:
            return False
        return await self._hdx.remove_member(team_api_key, email)

    async def revoke_member_everywhere(self, email: str) -> int:
        """Revoke a user's HyperDX membership across EVERY org.

        The account-level counterpart to ``revoke_member``: an account being
        disabled/deleted, or fully removed from its groups, loses access to all
        orgs, so its HyperDX membership is revoked on each org's team. Non-fatal;
        returns the number of revokes actually issued. A no-op (returns 0) when
        HyperDX is not wired or the email is empty.
        """
        if self._hdx is None or not email:
            return 0
        issued = 0
        for org in self._registry.list():
            if await self.revoke_member(org.name, email):
                issued += 1
        return issued

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

    def _get_team_api_key(self, path: str) -> str:
        """Read a HyperDX team API key back from the scalo.secrets seam.

        Mirrors ``_fixed_reader_secret``'s fail-soft shape: no store, no path, or a
        missing/errored secret -> empty string, so a revoke/invite silently no-ops
        instead of raising (HyperDX operations are non-fatal throughout this
        module).
        """
        if self._secrets is None or not path:
            return ""
        try:
            return self._secrets.get(path)
        except Exception:
            return ""

    def _existing_shared_team(self) -> tuple[str, str] | None:
        """The GA shared team (team_id, api_key_path) if a prior org already made it.

        GA posture (per_group False) puts every org on ONE HyperDX team, so the
        second org onwards must REUSE the first org's team rather than mint a
        duplicate ``dfe`` team. Returns the first stored (team_id, api_key_path) pair
        found in the registry, or None when no org has provisioned a team yet.
        """
        for existing in self._registry.list():
            if existing.hyperdx_team_id:
                return existing.hyperdx_team_id, existing.hyperdx_team_api_key_path
        return None

    async def _attach_tenant_connection(self, team_id: str, org: Org) -> str:
        """Attach the org's ClickHouse connection to its HyperDX team (tenant-reader model).

        The connection authenticates as the SHARED ``dfe_tenant_reader`` (secret
        ``ch/fixed/dfe_tenant_reader``) and carries the org's ``SQL_current_tenant_id``
        = comma-joined ``org_ids`` as a per-connection ClickHouse setting; the ONE
        restrictive row policy on each ``_org_id`` table scopes that reader to those
        orgs (empty setting -> 0 rows, fail closed). Network coordinates come from the
        ``default`` connection - every org hits the SAME ClickHouse as the SAME user,
        only the tenant setting differs. Non-fatal.

        Returns the created connection id (empty string on any failure) so the
        caller can persist it for a later posture-aware delete.
        """
        if self._conn_config is None:
            return ""
        base = self._conn_config.connections.get("default")
        if base is None:
            return ""
        try:
            conn_id = await self._hdx.create_connection(
                team_id=team_id,
                name=org.name,
                host=base.host,
                port=base.port,
                database=base.database,
                user=TENANT_READER_USER,
                password=self._fixed_reader_secret(),
                settings={TENANT_SETTING: ",".join(org.org_ids or [])},
            )
            return conn_id or ""
        except Exception as exc:
            logger.warning(
                "HyperDX connection create failed",
                org_name=org.name,
                error=str(exc),
            )
            return ""

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
                team_id, api_key_path = shared
                conn_id = await self._attach_tenant_connection(team_id, org)
                audit_org_hyperdx_provisioned(org_name=org.name, team_id=team_id)
                return self._registry.update(
                    org.name,
                    hyperdx_team_id=team_id,
                    hyperdx_team_api_key_path=api_key_path,
                    hyperdx_connection_id=conn_id,
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

        conn_id = await self._attach_tenant_connection(team_id, org)
        if conn_id:
            update_kwargs["hyperdx_connection_id"] = conn_id

        # Retrieve the team's own API key so we can invite members later. Minted
        # through the scalo.secrets seam (DfeSecrets), NOT the process environment:
        # os.environ is per-process (lost on restart) and per-pod (invisible to
        # sibling replicas in a multi-replica k8s deploy), whereas the secrets seam
        # is durable and shared. The path derives from the team name (shared under
        # GA, per-org otherwise). No secrets store configured -> non-fatal, matching
        # every other secrets-seam read/write in this module: the key is simply not
        # persisted (an invite attempt this run resolves no key and no-ops).
        team_api_key = await self._hdx.get_team_api_key(team_id)
        if team_api_key and self._secrets is not None:
            path = _team_api_key_secret_path(team_name)
            try:
                self._secrets.put(path, team_api_key)
                update_kwargs["hyperdx_team_api_key_path"] = path
                logger.info(
                    "HyperDX team API key stored",
                    org_name=org.name,
                    secret_path=path,
                )
            except Exception as exc:
                logger.warning(
                    "HyperDX team API key store failed",
                    org_name=org.name,
                    error=str(exc),
                )
        elif team_api_key:
            logger.warning(
                "HyperDX team API key minted but no secrets store configured; not persisted",
                org_name=org.name,
            )

        return self._registry.update(org.name, **update_kwargs)
