#  Project:      dfe-engine
#  File:         orgs/registry.py
#  Purpose:      YAML-backed CRUD store for customer organisations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""YAML-backed org registry for customer organisation management.

Orgs are stored as individual YAML files: ``{orgs_dir}/{name}.yaml``.
The org name is the filename stem -- it is NOT written into the YAML body.

Usage::

    from pathlib import Path
    from dfe_engine.orgs.registry import OrgRegistry

    registry = OrgRegistry(Path("/etc/dfe/orgs"))
    registry.create("acme", org_ids=["acme", "acme-sub"])
    org = registry.get("acme")
    registry.update("acme", display_name="Acme Corp")
    registry.delete("acme")
"""

import builtins
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from scalo.logger import logger

from dfe_engine.orgs.ingest_credentials import IngestCredentialRegistry
from dfe_engine.orgs.models import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_NAME,
    ORG_NAME_PATTERN,
    Org,
    ReservedOrgError,
)
from dfe_engine.orgs.tenant_scope import colliding_org, org_tenant_ids
from dfe_engine.yaml_utils import yaml_dump, yaml_load


class OrgBoundError(ValueError):
    """Another org's ingest credential still binds the org a delete names."""


class OrgExistsError(ValueError):
    """An org with the name a create names is already stored."""


class OrgRegistry:
    """YAML-backed store for customer organisations.

    Each org is a ``{name}.yaml`` file in ``orgs_dir``.
    The name is derived from the filename stem and is never stored
    inside the YAML body.

    The ingest credentials the orgs own are stored beside them, in
    ``{orgs_dir}/ingest-credentials``, and their writes call the same listeners.
    """

    def __init__(self, orgs_dir: Path) -> None:
        self._dir = Path(orgs_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._listeners: list[Callable[[], None]] = []
        self._removal_listeners: list[Callable[[str], None]] = []
        self.ingest_credentials = IngestCredentialRegistry(
            self._dir / "ingest-credentials", self._changed, self._credential_removed
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_change(self, listener: Callable[[], None]) -> None:
        """Call ``listener`` after every create, update and delete this registry makes.

        Every writer goes through here -- the API, the org lifecycle, the startup
        and e2e seeds -- so a listener cannot miss one. A listener that raises is
        logged and never fails the write.
        """
        self._listeners.append(listener)

    def _changed(self, name: str) -> None:
        """Tell every listener an org changed."""
        for listener in self._listeners:
            try:
                listener()
            except Exception as exc:
                logger.warning("org change listener failed", org=name, error=str(exc))

    def on_credential_removed(self, listener: Callable[[str], None]) -> None:
        """Call ``listener`` with the id of every ingest credential deleted.

        An org delete's cascade counts. A listener that raises is logged and never
        fails the delete.
        """
        self._removal_listeners.append(listener)

    def _credential_removed(self, name: str) -> None:
        for listener in self._removal_listeners:
            try:
                listener(name)
            except Exception as exc:
                logger.warning(
                    "ingest credential removal listener failed", ingest_id=name, error=str(exc)
                )

    def create(
        self,
        name: str,
        org_ids: builtins.list[str] | None = None,
        display_name: str = "",
    ) -> Org:
        """Create a new organisation.

        Args:
            name: Unique org name (used as filename stem).
            org_ids: Tenant IDs for ClickHouse row-level security.
            display_name: Human-readable label.

        Returns:
            The newly created Org.

        Raises:
            OrgExistsError: If an org with this name already exists.
            ValueError: If the name is not a valid org name, the name or any tenant
                id collides with another org's name or tenant ids, or a tenant id
                does not match ``TENANT_ID_PATTERN``.
        """
        if re.fullmatch(ORG_NAME_PATTERN, name) is None:
            raise ValueError(f"Invalid org name: {name!r}")
        path = self._path(name)
        if path.exists():
            raise OrgExistsError(f"Org already exists: {name}")
        other = colliding_org(name, org_ids or [], self.list())
        if other is not None:
            raise ValueError(f"Org '{name}' collides with tenant-fenced org '{other.name}'")

        now = _now()
        org = Org(
            name=name,
            display_name=display_name,
            org_ids=org_ids or [],
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        self._write(path, org)
        self._changed(name)
        return org

    def get(self, name: str) -> Org | None:
        """Return the org for *name*, or None if not found.

        Args:
            name: Org name to look up.

        Returns:
            Org if found, None otherwise.
        """
        try:
            path = self._path(name)
        except KeyError:
            return None
        if not path.exists():
            return None
        return self._read(path)

    def list(self) -> builtins.list[Org]:
        """Return all orgs in the store.

        Returns:
            List of Org objects (sorted by filename).
        """
        return [self._read(p) for p in sorted(self._dir.glob("*.yaml"))]

    def update(self, name: str, **fields: object) -> Org:
        """Update mutable fields on an existing org.

        Permitted fields: ``display_name``, ``org_ids``, ``enabled``,
        ``hyperdx_team_id``, ``hyperdx_team_api_key_env``, ``ch_password_env``.

        Args:
            name: Org to update.
            **fields: Fields to update.

        Returns:
            The updated Org.

        Raises:
            KeyError: If no org with *name* exists.
            ReservedOrgError: If the update would disable the default org or take
                its tenant id away.
            ValueError: If the update's tenant ids collide with another org's
                name or tenant ids, or one does not match ``TENANT_ID_PATTERN``.
        """
        path = self._path(name)
        if not path.exists():
            raise KeyError(name)

        org = self._read(path)
        refuse_reserved_update(org.model_copy(update=fields))

        new_org_ids = fields.get("org_ids")
        if isinstance(new_org_ids, list):
            other = colliding_org(name, new_org_ids, self.list())
            if other is not None:
                raise ValueError(f"Org '{name}' collides with tenant-fenced org '{other.name}'")

        update_dict: dict[str, object] = {}
        if "display_name" in fields:
            update_dict["display_name"] = fields["display_name"]
        if "org_ids" in fields:
            update_dict["org_ids"] = fields["org_ids"]
        if "enabled" in fields:
            update_dict["enabled"] = fields["enabled"]
        if "hyperdx_team_id" in fields:
            update_dict["hyperdx_team_id"] = fields["hyperdx_team_id"]
        if "hyperdx_team_api_key_env" in fields:
            update_dict["hyperdx_team_api_key_env"] = fields["hyperdx_team_api_key_env"]
        if "ch_password_env" in fields:
            update_dict["ch_password_env"] = fields["ch_password_env"]

        update_dict["updated_at"] = _now()
        org = Org.model_validate({**org.model_dump(), **update_dict})
        self._write(path, org)
        self._changed(name)
        return org

    def delete(self, name: str) -> None:
        """Remove an org, and the ingest credentials it owns with it.

        Args:
            name: Org to delete.

        Raises:
            KeyError: If no org with *name* exists.
            ReservedOrgError: If *name* is the default org.
            OrgBoundError: If another org's ingest credential binds *name*.
        """
        refuse_reserved_delete(name)
        path = self._path(name)
        if not path.exists():
            raise KeyError(name)
        refuse_bound_delete(name, self.ingest_credentials)
        for credential in self.ingest_credentials.owned_by(name):
            self.ingest_credentials.delete(credential.name)
        path.unlink()
        self._changed(name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, name: str) -> Path:
        """The file org *name* lives in.

        Raises:
            KeyError: No org can hold *name*, so it is looked up nowhere.
        """
        if re.fullmatch(ORG_NAME_PATTERN, name) is None:
            raise KeyError(name)
        return self._dir / f"{name}.yaml"

    def _read(self, path: Path) -> Org:
        """Load an Org from a YAML file.

        The name is derived from the filename stem -- it is not stored
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


def refuse_reserved_delete(name: str) -> None:
    """Refuse to delete the default org, which every unbound record lands in.

    Raises:
        ReservedOrgError: If *name* is the default org.
    """
    if name == DEFAULT_ORG_NAME:
        raise ReservedOrgError(
            f"Org '{DEFAULT_ORG_NAME}' is reserved: every record that names no org lands in "
            "it, so it cannot be deleted"
        )


def refuse_bound_delete(name: str, credentials: IngestCredentialRegistry) -> None:
    """Refuse to delete an org another org's ingest credential still binds.

    Its records would otherwise be held to an org that no longer exists.

    Raises:
        OrgBoundError: Naming each credential that binds *name*.
    """
    binding = credentials.binding(name)
    if binding:
        held = ", ".join(f"'{c.name}' of org '{c.org}'" for c in binding)
        raise OrgBoundError(
            f"Org '{name}' is bound by ingest credential {held}: take it out of their orgs first"
        )


def refuse_reserved_update(after: Org) -> None:
    """Refuse an update that would leave the default org disabled or without its tenant id.

    Args:
        after: The org as the update would write it.

    Raises:
        ReservedOrgError: If *after* is the default org and is disabled or has lost
            ``DEFAULT_ORG_ID``.
    """
    if after.name != DEFAULT_ORG_NAME:
        return
    if not after.enabled:
        raise ReservedOrgError(f"Org '{DEFAULT_ORG_NAME}' is reserved and cannot be disabled")
    if DEFAULT_ORG_ID not in org_tenant_ids(after):
        raise ReservedOrgError(
            f"Org '{DEFAULT_ORG_NAME}' is reserved and keeps the tenant id '{DEFAULT_ORG_ID}', "
            "which every app stamps on a record that names no org"
        )


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()
