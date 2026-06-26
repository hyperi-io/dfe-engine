#  Project:      DFE Engine
#  File:         orgs/ch_provisioner.py
#  Purpose:      ClickHouse user + database provisioner for dedicated org databases
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse user + database provisioner for dedicated org databases.

Only creates CH objects when an org has dedicated_database=True.
Shared DB orgs use the existing custom settings pattern (ConnectionRegistry).
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

from scalo.logger import logger


class OrgChProvisioner:
    """Provisions CH users + databases for orgs with dedicated databases.

    Non-fatal: logs errors but does not raise. Returns success/failure bool.
    """

    def __init__(self, ch_client: Any | None = None) -> None:
        self._client = ch_client

    def ch_user_name(self, org_name: str) -> str:
        safe = re.sub(r"[^a-z0-9_]", "_", org_name.lower())
        return f"dfe_org_{safe}"

    def database_name(self, org_name: str) -> str:
        safe = re.sub(r"[^a-z0-9_]", "_", org_name.lower())
        return f"dfe_{safe}"

    def generate_password(self) -> str:
        return secrets.token_urlsafe(24)[:32]

    def build_provision_sql(self, org_name: str, db_name: str) -> list[str]:
        user = self.ch_user_name(org_name)
        return [
            f"CREATE DATABASE IF NOT EXISTS {db_name}",
            f"CREATE USER IF NOT EXISTS {user} IDENTIFIED WITH sha256_hash BY '{{password_hash}}'",
            f"GRANT SELECT ON {db_name}.* TO {user}",
        ]

    def build_deprovision_sql(self, org_name: str, db_name: str) -> list[str]:
        user = self.ch_user_name(org_name)
        return [
            f"REVOKE ALL ON {db_name}.* FROM {user}",
            f"DROP USER IF EXISTS {user}",
        ]

    def provision(self, org_name: str) -> tuple[bool, str]:
        """Provision CH user + database. Returns (success, password)."""
        if self._client is None:
            logger.warning("No CH client configured, skipping org provisioning", org_name=org_name)
            return False, ""

        db_name = self.database_name(org_name)
        password = self.generate_password()

        try:
            pw_hash = hashlib.sha256(password.encode()).hexdigest()
            for stmt in self.build_provision_sql(org_name, db_name):
                sql = stmt.replace("{password_hash}", pw_hash)
                self._client.command(sql)
            logger.info(
                "CH org provisioned",
                org_name=org_name,
                user=self.ch_user_name(org_name),
                database=db_name,
            )
            return True, password
        except Exception as exc:
            logger.warning("CH org provisioning failed", org_name=org_name, error=str(exc))
            return False, ""

    def deprovision(self, org_name: str) -> bool:
        """Remove CH user (not database). Returns success."""
        if self._client is None:
            return False

        db_name = self.database_name(org_name)
        try:
            for stmt in self.build_deprovision_sql(org_name, db_name):
                self._client.command(stmt)
            logger.info("CH org deprovisioned", org_name=org_name)
            return True
        except Exception as exc:
            logger.warning("CH org deprovision failed", org_name=org_name, error=str(exc))
            return False
