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
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.orgs.ingest_credentials import IngestCredential, IngestCredentialRegistry
from dfe_engine.orgs.models import (
    ORG_NAME_PATTERN,
    UNASSIGNED_ORG_ID,
    UNASSIGNED_REFUSAL,
    HomeOrgError,
    Org,
)
from dfe_engine.orgs.tenant_scope import colliding_org, collision_refusal
from dfe_engine.yaml_utils import contained_yaml_file, yaml_dump, yaml_load_cached

if TYPE_CHECKING:
    # The store reads gitcrud, which loads settings, which loads this package.
    from dfe_engine.orgs.home import HomeOrgStore


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
        self._removal_listeners: list[Callable[[IngestCredential], None]] = []
        self.ingest_credentials = IngestCredentialRegistry(
            self._dir / "ingest-credentials", self._changed, self._credential_removed
        )
        # Where your org is named. None leaves the registry with no org of its own.
        self.home: HomeOrgStore | None = None

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

    def on_credential_removed(self, listener: Callable[[IngestCredential], None]) -> None:
        """Call ``listener`` with every ingest credential deleted, as it was stored.

        An org delete's cascade counts. A listener that raises is logged and never
        fails the delete.
        """
        self._removal_listeners.append(listener)

    def _credential_removed(self, credential: IngestCredential) -> None:
        for listener in self._removal_listeners:
            try:
                listener(credential)
            except Exception as exc:
                logger.warning(
                    "ingest credential removal listener failed",
                    ingest_id=credential.name,
                    error=str(exc),
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
        if UNASSIGNED_ORG_ID in (name, *(org_ids or [])):
            raise ValueError(UNASSIGNED_REFUSAL)
        other = colliding_org(name, org_ids or [], self.list())
        if other is not None:
            raise ValueError(collision_refusal(name=name, org_ids=org_ids or [], other=other)[0])

        now = _now()
        org = Org(
            name=name,
            display_name=display_name,
            # An org always carries an ID: with none given, its name.
            org_ids=org_ids or [name],
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
            HomeOrgError: If the update would disable your org.
            ValueError: If the update's tenant ids collide with another org's
                name or tenant ids, or one does not match ``TENANT_ID_PATTERN``.
        """
        path = self._path(name)
        if not path.exists():
            raise KeyError(name)

        org = self._read(path)
        self.refuse_home_update(after=org.model_copy(update=fields))

        new_org_ids = fields.get("org_ids")
        if isinstance(new_org_ids, list) and UNASSIGNED_ORG_ID in new_org_ids:
            raise ValueError(UNASSIGNED_REFUSAL)
        if isinstance(new_org_ids, list):
            other = colliding_org(name, new_org_ids, self.list())
            if other is not None:
                raise ValueError(collision_refusal(name=name, org_ids=new_org_ids, other=other)[0])

        update_dict: dict[str, object] = {}
        if "display_name" in fields:
            update_dict["display_name"] = fields["display_name"]
        if "org_ids" in fields:
            update_dict["org_ids"] = fields["org_ids"] or [name]
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
            HomeOrgError: If *name* is your org.
            OrgBoundError: If another org's ingest credential binds *name*.
        """
        self.refuse_home_delete(name=name)
        path = self._path(name)
        if not path.exists():
            raise KeyError(name)
        refuse_bound_delete(name, self.ingest_credentials)
        for credential in self.ingest_credentials.owned_by(name):
            self.ingest_credentials.delete(credential.name)
        path.unlink()
        self._changed(name)

    def home_name(self) -> str | None:
        """Your org's name as its doc names it, None with no store or none named. Raises ``ValueError`` when the doc cannot be read."""
        return self.home.get() if self.home is not None else None

    def home_name_known(self) -> str | None:
        """Your org's name as this replica last read or wrote it, for a caller that must not wait on the deploy repo."""
        return self.home.known if self.home is not None else None

    def refuse_home_delete(self, *, name: str) -> None:
        """Raise ``HomeOrgError`` when *name* is your org, which records naming no org land in."""
        if self._is_home(name=name):
            raise HomeOrgError(
                f"Org {name!r} is your organisation: name another one as yours before deleting it"
            )

    def refuse_home_update(self, *, after: Org) -> None:
        """Raise ``HomeOrgError`` when *after*, the org as an update would write it, is your org disabled."""
        if (not (after.enabled)) and self._is_home(name=after.name):
            raise HomeOrgError(f"Org {after.name!r} is your organisation and cannot be disabled")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_home(self, *, name: str) -> bool:
        """Whether *name* is your org. An unreadable doc refuses with ``HomeOrgError``, since it may name *name*."""
        try:
            return self.home_name() == name
        except ValueError as exc:
            raise HomeOrgError(
                f"Your organisation cannot be read, so no org is deleted or disabled until "
                f"PUT /api/v1/system/organisation replaces its doc: {exc}"
            ) from exc

    def _path(self, name: str) -> Path:
        """The file org *name* lives in.

        Raises:
            KeyError: No org can hold *name*, or its file resolves outside the
                directory, so it is looked up nowhere.
        """
        if re.fullmatch(ORG_NAME_PATTERN, name) is None:
            raise KeyError(name)
        path = contained_yaml_file(self._dir, name)
        if path is None:
            raise KeyError(name)
        return path

    def _read(self, path: Path) -> Org:
        """Load an Org from a YAML file.

        The name is derived from the filename stem -- it is not stored
        inside the YAML body.
        """
        data: dict = yaml_load_cached(path)
        data["name"] = path.stem
        return Org.model_validate(data)

    def _write(self, path: Path, org: Org) -> None:
        """Persist an Org to YAML, omitting the name field."""
        data = org.model_dump(exclude={"name"})
        yaml_dump(data, path)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


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


def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()
