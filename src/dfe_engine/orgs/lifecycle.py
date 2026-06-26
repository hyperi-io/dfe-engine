#  Project:      dfe-engine
#  File:         orgs/lifecycle.py
#  Purpose:      Orchestrates org lifecycle across registry, CH provisioner, and HyperDX
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Org lifecycle manager.

Orchestrates org creation, deletion, and configuration changes across
the OrgRegistry, OrgChProvisioner, and HyperDXClient.  CH and HyperDX
operations are non-fatal — failures are logged as warnings and the org
operation proceeds.

Usage::

    from pathlib import Path
    from dfe_engine.orgs.lifecycle import OrgLifecycleManager
    from dfe_engine.orgs.registry import OrgRegistry

    registry = OrgRegistry(Path("/etc/dfe/orgs"))
    manager = OrgLifecycleManager(registry)
    org = await manager.create_org("acme", org_ids=["acme"], dedicated_database=True, admin_id="admin")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.auth.audit import (
    audit_org_ch_failed,
    audit_org_ch_provisioned,
    audit_org_change,
    audit_org_hyperdx_failed,
    audit_org_hyperdx_provisioned,
)
from dfe_engine.orgs.models import Org
from dfe_engine.orgs.registry import OrgRegistry

if TYPE_CHECKING:
    from dfe_engine.connections.config import ConnectionConfig
    from dfe_engine.hyperdx.client import HyperDXClient
    from dfe_engine.orgs.ch_provisioner import OrgChProvisioner


class OrgLifecycleManager:
    """Orchestrates org lifecycle across OrgRegistry, OrgChProvisioner, and HyperDXClient.

    CH and HyperDX operations are non-fatal: failures are logged and do not
    prevent the org operation from completing.
    """

    def __init__(
        self,
        registry: OrgRegistry,
        ch_provisioner: OrgChProvisioner | None = None,
        hyperdx_client: HyperDXClient | None = None,
        connection_config: ConnectionConfig | None = None,
    ) -> None:
        self._registry = registry
        self._ch = ch_provisioner
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
        dedicated_database: bool = False,
        admin_id: str,
    ) -> Org:
        """Create a new org and provision external resources.

        Creates the org in the registry, optionally provisions a dedicated
        ClickHouse database, and creates a HyperDX team.  CH and HyperDX
        failures are non-fatal.

        Args:
            name: Unique org name.
            org_ids: Tenant IDs for ClickHouse row-level security.
            display_name: Human-readable label.
            dedicated_database: Whether to provision a dedicated CH database.
            admin_id: Identity of the admin performing the operation.

        Returns:
            The newly created Org.

        Raises:
            ValueError: If an org with this name already exists.
        """
        org = self._registry.create(name, org_ids=org_ids, display_name=display_name)

        if dedicated_database:
            org = await self._enable_dedicated_db(org)

        org = await self._provision_hyperdx(org)

        audit_org_change(admin_id=admin_id, org_name=name, change="created")
        return org

    async def delete_org(self, name: str, *, admin_id: str) -> None:
        """Delete an org and deprovision external resources.

        Deprovisions the ClickHouse user (if dedicated database) and
        deletes the HyperDX team before removing the org from the registry.
        CH and HyperDX failures are non-fatal.

        Args:
            name: Org to delete.
            admin_id: Identity of the admin performing the operation.

        Raises:
            KeyError: If no org with *name* exists.
        """
        org = self._registry.get(name)
        if org is None:
            raise KeyError(name)

        if org.dedicated_database and self._ch is not None:
            self._ch.deprovision(name)

        if org.hyperdx_team_id and self._hdx is not None:
            await self._hdx.delete_team(org.hyperdx_team_id)

        self._registry.delete(name)
        audit_org_change(admin_id=admin_id, org_name=name, change="deleted")

    async def toggle_dedicated_db(
        self,
        name: str,
        *,
        enabled: bool,
        confirm_merge: bool = False,
        admin_id: str,
    ) -> Org:
        """Enable or disable a dedicated ClickHouse database for an org.

        When disabling, ``confirm_merge`` must be True to acknowledge that
        data migration is the caller's responsibility.

        Args:
            name: Org to update.
            enabled: True to enable dedicated database, False to disable.
            confirm_merge: Required when disabling — caller confirms they
                have handled data migration.
            admin_id: Identity of the admin performing the operation.

        Returns:
            The updated Org.

        Raises:
            KeyError: If no org with *name* exists.
            ValueError: If disabling without ``confirm_merge=True``.
        """
        org = self._registry.get(name)
        if org is None:
            raise KeyError(name)

        if not enabled and not confirm_merge:
            raise ValueError(
                "confirm_merge=True is required when disabling dedicated_database "
                "to acknowledge that data migration is the caller's responsibility."
            )

        if enabled:
            org = await self._enable_dedicated_db(org)
        else:
            org = self._registry.update(name, dedicated_database=False)

        audit_org_change(
            admin_id=admin_id,
            org_name=name,
            change="updated",
            details={"dedicated_database": enabled},
        )
        return org

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _enable_dedicated_db(self, org: Org) -> Org:
        """Provision a dedicated CH database and update the org record.

        Non-fatal: if provisioning fails, the org is still updated with
        ``dedicated_database=True`` and an empty ``database_name``.

        Args:
            org: The org to provision.

        Returns:
            Updated Org with dedicated_database=True and database_name set.
        """
        if self._ch is None:
            logger.warning(
                "No CH provisioner configured, skipping dedicated DB setup",
                org_name=org.name,
            )
            return self._registry.update(
                org.name,
                dedicated_database=True,
            )

        db_name = self._ch.database_name(org.name)
        ch_user = self._ch.ch_user_name(org.name)
        success, _password = self._ch.provision(org.name)

        if success:
            audit_org_ch_provisioned(
                org_name=org.name,
                ch_user=ch_user,
                databases=[db_name],
            )
        else:
            audit_org_ch_failed(
                org_name=org.name,
                error="CH provisioning returned failure",
            )

        return self._registry.update(
            org.name,
            dedicated_database=True,
            database_name=db_name if success else "",
        )

    async def _provision_hyperdx(self, org: Org) -> Org:
        """Create a HyperDX team for the org and persist the team ID.

        Non-fatal: if HyperDX is unavailable, the org record is unchanged.

        Args:
            org: The org to provision.

        Returns:
            Updated Org with hyperdx_team_id set (unchanged on failure).
        """
        if self._hdx is None:
            return org

        try:
            team_id = await self._hdx.create_team(f"customer-{org.name}")
        except Exception as exc:
            audit_org_hyperdx_failed(org_name=org.name, error=str(exc))
            return org

        if team_id:
            audit_org_hyperdx_provisioned(org_name=org.name, team_id=team_id)
            update_kwargs: dict[str, object] = {"hyperdx_team_id": team_id}

            # Attach the tenant_reader ClickHouse connection to the new team so
            # the org's HyperDX can query its data (non-fatal).
            if self._conn_config is not None:
                conn = self._conn_config.connections.get("tenant_reader")
                if conn is not None:
                    import os

                    try:
                        await self._hdx.create_connection(
                            team_id=team_id,
                            name="tenant_reader",
                            host=conn.host,
                            port=conn.port,
                            database=conn.database,
                            user=conn.user,
                            password=os.environ.get(conn.password_env, ""),
                        )
                    except Exception as exc:
                        logger.warning(
                            "HyperDX connection create failed",
                            org_name=org.name,
                            error=str(exc),
                        )

            # Retrieve the team's own API key so we can invite members later.
            # The env var name follows the same convention as ch_password_env.
            team_api_key = await self._hdx.get_team_api_key(team_id)
            if team_api_key:
                env_var = f"HYPERDX_TEAM_API_KEY_{org.name.upper().replace('-', '_')}"
                import os

                os.environ[env_var] = team_api_key
                update_kwargs["hyperdx_team_api_key_env"] = env_var
                logger.info(
                    "HyperDX team API key stored",
                    org_name=org.name,
                    env_var=env_var,
                )

            return self._registry.update(org.name, **update_kwargs)

        audit_org_hyperdx_failed(org_name=org.name, error="create_team returned None")
        return org
