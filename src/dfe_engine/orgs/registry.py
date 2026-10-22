#  Project:      dfe-engine
#  File:         orgs/registry.py
#  Purpose:      YAML-backed CRUD store for customer organisations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Org registries for customer organisation management.

:class:`OrgRegistry` stores orgs as individual YAML files, ``{orgs_dir}/{name}.yaml``,
with the org name as the filename stem and never in the YAML body. It serves one
engine. :class:`DocuStoreOrgRegistry` stores them in the document store, where every
engine replica reads and writes the same orgs.

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
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from scalo.logger import logger

from dfe_engine.orgs.ingest_credentials import (
    DocuStoreIngestCredentialRegistry,
    IngestCredentialRegistry,
)
from dfe_engine.orgs.models import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_NAME,
    ORG_NAME_PATTERN,
    Org,
    ReservedOrgError,
)
from dfe_engine.orgs.tenant_scope import colliding_org, org_tenant_ids
from dfe_engine.store.documents import DEFAULT_ATTEMPTS, Claim, DocumentConflictError, DocuStore
from dfe_engine.yaml_utils import contained_yaml_file, yaml_dump, yaml_load

TENANT_KEYS_COLLECTION = "tenant_keys"
"""Where :class:`DocuStoreOrgRegistry` claims each org's name and tenant ids."""

CLAIM_GRACE_SECONDS = 60.0
"""How old a name or tenant id claim no org record holds must be before it is released.

Longer than any org write takes between claiming a key and writing the record that
holds it, so a write still in flight keeps its claims.
"""

# The fields an update may change. Anything else it names is ignored.
_UPDATABLE = (
    "display_name",
    "org_ids",
    "enabled",
    "hyperdx_team_id",
    "hyperdx_team_api_key_env",
    "ch_password_env",
)


class OrgBoundError(ValueError):
    """Another org's ingest credential still binds the org a delete names."""


class OrgExistsError(ValueError):
    """An org with the name a create names is already stored."""


class OrgCollisionError(ValueError):
    """A write's org name or tenant ids are another org's name or tenant ids."""


class _OrgEvents:
    """The change and credential-removal listeners every org registry calls."""

    def __init__(self) -> None:
        self._listeners: list[Callable[[], None]] = []
        self._removal_listeners: list[Callable[[str], None]] = []

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


class OrgRegistry(_OrgEvents):
    """YAML-backed store for customer organisations, for one engine.

    Each org is a ``{name}.yaml`` file in ``orgs_dir``.
    The name is derived from the filename stem and is never stored
    inside the YAML body.

    The ingest credentials the orgs own are stored beside them, in
    ``{orgs_dir}/ingest-credentials``, and their writes call the same listeners.
    """

    def __init__(self, orgs_dir: Path) -> None:
        super().__init__()
        self._dir = Path(orgs_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.ingest_credentials = IngestCredentialRegistry(
            self._dir / "ingest-credentials", self._changed, self._credential_removed
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
            OrgCollisionError: If the name or any tenant id collides with another
                org's name or tenant ids.
            ValueError: If the name is not a valid org name, or a tenant id does not
                match ``TENANT_ID_PATTERN``.
        """
        if re.fullmatch(ORG_NAME_PATTERN, name) is None:
            raise ValueError(f"Invalid org name: {name!r}")
        path = self._path(name)
        if path.exists():
            raise OrgExistsError(f"Org already exists: {name}")
        other = colliding_org(name, org_ids or [], self.list())
        if other is not None:
            raise OrgCollisionError(_collision(name, other.name))

        org = _new_org(name, org_ids, display_name)
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
            OrgCollisionError: If the update's tenant ids collide with another org's
                name or tenant ids.
            ValueError: If a tenant id does not match ``TENANT_ID_PATTERN``.
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
                raise OrgCollisionError(_collision(name, other.name))

        org = _updated(org, fields)
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
        data: dict = yaml_load(path)
        data["name"] = path.stem
        return Org.model_validate(data)

    def _write(self, path: Path, org: Org) -> None:
        """Persist an Org to YAML, omitting the name field."""
        data = org.model_dump(exclude={"name"})
        yaml_dump(data, path)


class DocuStoreOrgRegistry(_OrgEvents):
    """Orgs in the document store, one document per org, for every engine replica.

    The interface of :class:`OrgRegistry`. Each org's name and tenant ids are claimed
    in :data:`TENANT_KEYS_COLLECTION` before its record is written, so a claim, not a
    read of every org, decides who holds one. Of two replicas creating orgs that share
    a tenant id, exactly one succeeds.

    A claim is released only as it reads, so one a write in flight renewed since is
    kept. A claim no org record holds is released once it is older than the grace:
    when a write contends for its key, and at :meth:`release_orphaned_claims`. That
    covers a replica that died between a claim and its record, and a key an update
    or delete gave up within the grace.
    """

    def __init__(
        self,
        store: DocuStore,
        *,
        collection: str = "orgs",
        claim_grace_seconds: float = CLAIM_GRACE_SECONDS,
    ) -> None:
        super().__init__()
        self._orgs = store.typed(collection, Org, key="name")
        self._keys = store.claims(TENANT_KEYS_COLLECTION)
        self._grace = claim_grace_seconds
        self.ingest_credentials = DocuStoreIngestCredentialRegistry(
            store, self._changed, self._credential_removed
        )

    def create(
        self,
        name: str,
        org_ids: builtins.list[str] | None = None,
        display_name: str = "",
    ) -> Org:
        """Create a new organisation, as :meth:`OrgRegistry.create` does.

        Raises:
            OrgExistsError: If an org with this name already exists.
            OrgCollisionError: If another org holds the name or a tenant id.
            ValueError: If the name is not a valid org name, or a tenant id does not
                match ``TENANT_ID_PATTERN``.
            DocumentConflictError: Concurrent writers beat every attempt.
        """
        if re.fullmatch(ORG_NAME_PATTERN, name) is None:
            raise ValueError(f"Invalid org name: {name!r}")
        if self._orgs.exists(name):
            raise OrgExistsError(f"Org already exists: {name}")
        org = _new_org(name, org_ids, display_name)
        made = self._claim(name, _markers(org))
        try:
            if not self._orgs.insert(org):
                raise OrgExistsError(f"Org already exists: {name}")
        except BaseException:
            self._withdraw(made)
            raise
        self._changed(name)
        return org

    def get(self, name: str) -> Org | None:
        """Return the org for *name*, or None if not found."""
        return self._orgs.get(name)

    def list(self) -> builtins.list[Org]:
        """Return all orgs, sorted by name."""
        return self._orgs.list()

    def update(self, name: str, **fields: object) -> Org:
        """Update mutable fields on an existing org, as :meth:`OrgRegistry.update` does.

        Raises:
            KeyError: If no org with *name* exists.
            ReservedOrgError: If the update would disable the default org or take
                its tenant id away.
            OrgCollisionError: If another org holds one of the update's tenant ids.
            ValueError: If a tenant id does not match ``TENANT_ID_PATTERN``.
            DocumentConflictError: Concurrent writers beat every attempt.
        """
        if not self._orgs.exists(name):
            raise KeyError(name)
        new_org_ids = fields.get("org_ids")
        made = self._claim(name, {name, *new_org_ids}) if isinstance(new_org_ids, list) else []
        replaced: builtins.list[Org] = []

        def change(current: Org) -> Org:
            refuse_reserved_update(current.model_copy(update=fields))
            replaced[:] = [current]
            return _updated(current, fields)

        try:
            org = self._orgs.mutate(name, change)
        except BaseException:
            self._withdraw(made)
            raise
        self._release_unheld(name, _markers(replaced[0]) - _markers(org))
        self._changed(name)
        return org

    def delete(self, name: str) -> None:
        """Remove an org and the ingest credentials it owns, as :meth:`OrgRegistry.delete` does.

        Raises:
            KeyError: If no org with *name* exists.
            ReservedOrgError: If *name* is the default org.
            OrgBoundError: If another org's ingest credential binds *name*.
        """
        refuse_reserved_delete(name)
        if not self._orgs.exists(name):
            raise KeyError(name)
        refuse_bound_delete(name, self.ingest_credentials)
        for credential in self.ingest_credentials.owned_by(name):
            self.ingest_credentials.delete(credential.name)
        if not self._orgs.delete(name):
            raise KeyError(name)
        self._release_unheld(name, {claim.key for claim in self._keys.owned_by(name)})
        self._changed(name)

    def release_orphaned_claims(self) -> builtins.list[str]:
        """Release every claim no org record holds that is older than the grace.

        Returns:
            The keys released.
        """
        return [
            claim.key
            for claim in self._keys.list()
            if self._keys.release_if_orphaned(
                claim, held=self._holds(claim), grace_seconds=self._grace
            )
        ]

    def _claim(self, owner: str, markers: Iterable[str]) -> builtins.list[Claim]:
        """Claim every marker for org *owner*, and return the claims this call made.

        A marker *owner* already holds is renewed and not returned, so undoing a failed
        write never takes away a claim an earlier write made.

        Raises:
            OrgCollisionError: Another org holds a marker. Every claim made is withdrawn.
        """
        made: builtins.list[Claim] = []
        try:
            for marker in sorted(markers):
                claim = self._take(marker, owner)
                if claim is not None:
                    made.append(claim)
        except BaseException:
            self._withdraw(made)
            raise
        return made

    def _take(self, marker: str, owner: str) -> Claim | None:
        """Claim *marker* for *owner*. The claim when this call made it, None when renewed.

        A claim another org holds is released first when that org's record does not
        hold the marker and the claim is older than the grace.

        Raises:
            OrgCollisionError: Another org holds *marker*.
        """
        for _ in range(DEFAULT_ATTEMPTS):
            made = self._keys.create(marker, owner)
            if made is not None:
                return made
            held = self._keys.get(marker)
            if held is None:
                continue  # released since the create found it
            if held.owner == owner:
                if self._keys.claim(marker, owner):
                    return None
                continue  # taken by another org since the read
            if not self._keys.release_if_orphaned(
                held, held=self._holds(held), grace_seconds=self._grace
            ):
                raise OrgCollisionError(_collision(owner, held.owner))
        raise DocumentConflictError(f"tenant key '{marker}' changed hands under every attempt")

    def _holds(self, claim: Claim) -> bool:
        """Whether the record of the org holding *claim* holds its key."""
        org = self._orgs.get(claim.owner)
        return org is not None and claim.key in _markers(org)

    def _withdraw(self, made: Iterable[Claim]) -> None:
        """Withdraw claims a failed write made. One that will not go is left to the grace."""
        for claim in made:
            try:
                self._keys.withdraw(claim)
            except Exception as exc:
                logger.warning("tenant key claim not withdrawn", key=claim.key, error=str(exc))

    def _release_unheld(self, owner: str, keys: Iterable[str]) -> None:
        """Release each of *owner*'s claims on *keys* its record no longer holds, past the grace."""
        for key in keys:
            claim = self._keys.get(key)
            if claim is not None and claim.owner == owner:
                self._keys.release_if_orphaned(
                    claim, held=self._holds(claim), grace_seconds=self._grace
                )


type AnyOrgRegistry = OrgRegistry | DocuStoreOrgRegistry
"""Either org registry: YAML for one engine, the document store for replicas."""


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _new_org(name: str, org_ids: builtins.list[str] | None, display_name: str) -> Org:
    now = _now()
    return Org(
        name=name,
        display_name=display_name,
        org_ids=org_ids or [],
        enabled=True,
        created_at=now,
        updated_at=now,
    )


def _updated(org: Org, fields: Mapping[str, object]) -> Org:
    """*org* with the updatable *fields* applied and ``updated_at`` stamped."""
    changes = {field: fields[field] for field in _UPDATABLE if field in fields}
    return Org.model_validate({**org.model_dump(), **changes, "updated_at": _now()})


def _markers(org: Org) -> set[str]:
    """The keys an org holds against every other org: its name and its tenant ids."""
    return {org.name, *org_tenant_ids(org)}


def _collision(name: str, other: str) -> str:
    return f"Org '{name}' collides with tenant-fenced org '{other}'"


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


def refuse_bound_delete(
    name: str, credentials: IngestCredentialRegistry | DocuStoreIngestCredentialRegistry
) -> None:
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
