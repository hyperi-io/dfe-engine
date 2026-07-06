#  Project:      dfe-engine
#  File:         orgs/registry.py
#  Purpose:      YAML-backed CRUD store for customer organisations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""YAML-backed org registry for customer organisation management.

Orgs are stored as individual YAML files: ``{orgs_dir}/{name}.yaml``.
The org name is the filename stem — it is NOT written into the YAML body.

Usage::

    from pathlib import Path
    from dfe_engine.orgs.registry import OrgRegistry

    registry = OrgRegistry(Path("/etc/dfe/orgs"))
    registry.create("acme", org_ids=["acme", "acme-sub"])
    org = registry.get("acme")
    registry.update("acme", display_name="Acme Corp")
    registry.delete("acme")
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from scalo.logger import logger

from dfe_engine.orgs.models import Org
from dfe_engine.yaml_utils import yaml_dump, yaml_load


class OrgRegistry:
    """YAML-backed store for customer organisations.

    Each org is a ``{name}.yaml`` file in ``orgs_dir``.
    The name is derived from the filename stem and is never stored
    inside the YAML body.
    """

    def __init__(self, orgs_dir: Path) -> None:
        self._dir = Path(orgs_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        org_ids: list[str] | None = None,
        display_name: str = "",
        *,
        domains: list[str] | None = None,
    ) -> Org:
        """Create a new organisation.

        Args:
            name: Unique org name (used as filename stem).
            org_ids: Tenant IDs for ClickHouse row-level security.
            display_name: Human-readable label.
            domains: Email domains this org claims (lowercased on write).

        Returns:
            The newly created Org.

        Raises:
            ValueError: If an org with this name already exists.
        """
        path = self._path(name)
        if path.exists():
            raise ValueError(f"Org already exists: {name}")

        now = _now()
        org = Org(
            name=name,
            display_name=display_name,
            org_ids=org_ids or [],
            domains=_normalise_domains(domains),
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        self._write(path, org)
        return org

    def get(self, name: str) -> Org | None:
        """Return the org for *name*, or None if not found.

        Args:
            name: Org name to look up.

        Returns:
            Org if found, None otherwise.
        """
        path = self._path(name)
        if not path.exists():
            return None
        return self._read(path)

    def list(self) -> list[Org]:
        """Return all orgs in the store.

        Returns:
            List of Org objects (sorted by filename).
        """
        return [self._read(p) for p in sorted(self._dir.glob("*.yaml"))]

    def find_by_domain(self, domain: str) -> Org | None:
        """Return the org that claims *domain* (case-insensitive), or None.

        A domain should be claimed by AT MOST ONE org, so this is the
        email-domain -> org resolver JIT uses to bind an external login to its
        tenant scope. Unclaimed domain -> None (the caller then provisions a
        domain group with no org_ids -> no tenant access).

        Tie-break: if two+ orgs list the same domain (a misconfiguration), we log
        a warning and pick DETERMINISTICALLY the org whose name sorts first
        (ascending), so resolution is stable across runs and independent of
        filesystem ordering.

        A DISABLED org never claims a domain (parity with the HyperDX sync, which
        already skips disabled orgs). Stored domains are re-normalised on compare,
        not trusted to be lowercase, so a hand/gitops-written record (survivability
        path, no create/update validator) still resolves.
        """
        target = domain.strip().lower()
        if not target:
            return None
        matches = sorted(
            (
                o
                for o in self.list()
                if o.enabled and target in {d.strip().lower() for d in o.domains}
            ),
            key=lambda o: o.name,
        )
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "Email domain claimed by multiple orgs; picking first by name",
                domain=target,
                orgs=[o.name for o in matches],
                chosen=matches[0].name,
            )
        return matches[0]

    def update(self, name: str, **fields: object) -> Org:
        """Update mutable fields on an existing org.

        Permitted fields: ``display_name``, ``org_ids``, ``domains``, ``enabled``,
        ``hyperdx_team_id``, ``hyperdx_team_api_key_env``, ``hyperdx_connection_id``,
        ``ch_password_env``.

        Args:
            name: Org to update.
            **fields: Fields to update.

        Returns:
            The updated Org.

        Raises:
            KeyError: If no org with *name* exists.
        """
        path = self._path(name)
        if not path.exists():
            raise KeyError(name)

        org = self._read(path)

        update_dict: dict[str, object] = {}
        if "display_name" in fields:
            update_dict["display_name"] = fields["display_name"]
        if "org_ids" in fields:
            update_dict["org_ids"] = fields["org_ids"]
        if "domains" in fields:
            # model_copy(update=...) skips validators, so normalise here - this is
            # the "lowercase on set" point for the update path. fields is
            # **object, so narrow the domains value back to the list it must be.
            update_dict["domains"] = _normalise_domains(cast("list[str] | None", fields["domains"]))
        if "enabled" in fields:
            update_dict["enabled"] = fields["enabled"]
        if "hyperdx_team_id" in fields:
            update_dict["hyperdx_team_id"] = fields["hyperdx_team_id"]
        if "hyperdx_team_api_key_env" in fields:
            update_dict["hyperdx_team_api_key_env"] = fields["hyperdx_team_api_key_env"]
        if "hyperdx_connection_id" in fields:
            update_dict["hyperdx_connection_id"] = fields["hyperdx_connection_id"]
        if "ch_password_env" in fields:
            update_dict["ch_password_env"] = fields["ch_password_env"]

        update_dict["updated_at"] = _now()
        org = org.model_copy(update=update_dict)
        self._write(path, org)
        return org

    def delete(self, name: str) -> None:
        """Remove an org.

        Args:
            name: Org to delete.

        Raises:
            KeyError: If no org with *name* exists.
        """
        path = self._path(name)
        if not path.exists():
            raise KeyError(name)
        path.unlink()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self._dir / f"{name}.yaml"

    def _read(self, path: Path) -> Org:
        """Load an Org from a YAML file.

        The name is derived from the filename stem — it is not stored
        inside the YAML body.
        """
        data: dict = yaml_load(path)
        data["name"] = path.stem
        return Org.model_validate(data)

    def _write(self, path: Path, org: Org) -> None:
        """Persist an Org to YAML, omitting the name field."""
        data = org.model_dump(exclude={"name"})
        yaml_dump(data, path)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _normalise_domains(domains: list[str] | None) -> list[str]:
    """Lowercase + strip email domains, dropping blanks and duplicates.

    Order-preserving so the stored list reads back in the order supplied. This is
    the single "lowercase on set" point shared by create() and update() so a
    domain matches ``find_by_domain`` regardless of the case it was entered in.
    """
    if not domains:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for raw in domains:
        cleaned = str(raw).strip().lower()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result
