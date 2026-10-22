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
import contextlib
import re
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

from scalo.logger import logger

from dfe_engine.orgs.ingest_credentials import (
    DocuStoreIngestCredentialRegistry,
    IngestCredential,
    IngestCredentialRegistry,
    OrgUnavailableError,
)
from dfe_engine.orgs.models import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_NAME,
    ORG_NAME_PATTERN,
    Org,
    ReservedOrgError,
)
from dfe_engine.orgs.tenant_scope import colliding_org, org_tenant_ids
from dfe_engine.store.documents import (
    DEFAULT_ATTEMPTS,
    Claim,
    Claims,
    DocumentConflictError,
    DocuStore,
)
from dfe_engine.yaml_utils import contained_yaml_file, yaml_dump, yaml_load

TENANT_KEYS_COLLECTION = "tenant_keys"
"""Where :class:`DocuStoreOrgRegistry` claims each org's name and tenant ids."""

BINDINGS_COLLECTION = "org_bindings"
"""Where each document-store credential claims the orgs it binds, its owner among them."""

DELETES_COLLECTION = "org_deletes"
"""Where a delete marks the org it is removing, so no credential write binds it meanwhile."""

CREATES_COLLECTION = "org_creates"
"""Where a create held across its hunt write locks the org name against another create."""

CLAIM_GRACE_SECONDS = 60.0
"""How old a claim no record holds must be before it is released.

Longer than any write takes between claiming a key and writing the record that holds
it, so a write still in flight keeps its claims. A key an update or delete gives up is
held this long after its last claim, so taking it straight back for another org is
refused until then.
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

    @contextlib.contextmanager
    def reserved(self, name: str, org_ids: Iterable[str]) -> Iterator[None]:
        """Refuse a create that collides with another org, before its hunts are written.

        One engine writes these files and its writes take turns, so nothing needs holding.

        Raises:
            OrgExistsError: An org has the name.
            OrgCollisionError: Another org holds the name or a tenant id.
        """
        if self._path(name).exists():
            raise OrgExistsError(f"Org already exists: {name}")
        other = colliding_org(name, org_ids, self.list())
        if other is not None:
            raise OrgCollisionError(_collision(name, other.name))
        yield

    @contextlib.contextmanager
    def deleting(self, name: str) -> Iterator[None]:
        """Refuse a delete the org's dependants forbid, before its hunts are written.

        Raises:
            KeyError: No org has the name.
            ReservedOrgError: It is the default org.
            OrgBoundError: Another org's ingest credential binds it.
        """
        refuse_reserved_delete(name)
        if not self._path(name).exists():
            raise KeyError(name)
        refuse_bound_delete(name, self.ingest_credentials)
        yield

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

    A credential write claims each org it binds, its owner among them, in
    :data:`BINDINGS_COLLECTION` and then checks the org is not being deleted. A delete
    marks the org in :data:`DELETES_COLLECTION` and then looks for credentials and
    claims binding it. Whichever comes second sees the other, so an org is never
    deleted while a credential write binds it or creates one it owns.
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
        self._bindings = store.claims(BINDINGS_COLLECTION)
        self._deletes = store.claims(DELETES_COLLECTION)
        self._creates = store.claims(CREATES_COLLECTION)
        # The orgs this registry's deleting() holds, whose bindings were checked under their mark.
        self._held_deletes: set[str] = set()
        self._grace = claim_grace_seconds
        self.ingest_credentials = DocuStoreIngestCredentialRegistry(
            store, self._changed, self._credential_removed, binder=self
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
        # A stored org whose claims were lost still collides here. The claims decide races.
        other = colliding_org(name, org_ids or [], self.list())
        if other is not None:
            raise OrgCollisionError(_collision(name, other.name))
        org = _new_org(name, org_ids, display_name)
        made = self._claim(name, _markers(org))
        try:
            if not self._orgs.insert(org):
                raise OrgExistsError(f"Org already exists: {name}")
        except BaseException:
            self._withdraw_unheld(made)
            raise
        self._changed(name)
        return org

    @contextlib.contextmanager
    def reserved(self, name: str, org_ids: Iterable[str]) -> Iterator[None]:
        """Hold the name and tenant ids of a create while its hunts are written, then its record.

        The name is locked against every other create held this way, and the name and
        tenant ids are claimed as :meth:`create` claims them, so a create that would lose
        is refused here, before it has written anything. A failure inside takes back the
        claims this made that no stored org holds.

        Raises:
            OrgExistsError: An org has the name, or another create holds it.
            OrgCollisionError: Another org holds the name or a tenant id.
        """
        if re.fullmatch(ORG_NAME_PATTERN, name) is None:
            raise ValueError(f"Invalid org name: {name!r}")
        if self._orgs.exists(name):
            raise OrgExistsError(f"Org already exists: {name}")
        lock = self._lock_create(name)
        try:
            made = self._claim(name, {name, *org_ids})
            try:
                yield
            except BaseException:
                self._withdraw_unheld(made)
                raise
        finally:
            _withdraw(self._creates, [lock])

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
            self._withdraw_unheld(made)
            raise
        self._release_unheld(name, _markers(replaced[0]) - _markers(org))
        self._changed(name)
        return org

    def delete(self, name: str) -> None:
        """Remove an org and the ingest credentials it owns, as :meth:`OrgRegistry.delete` does.

        Raises:
            KeyError: If no org with *name* exists.
            ReservedOrgError: If *name* is the default org.
            OrgBoundError: If another org's ingest credential binds *name*, or a write
                binding it is in flight.
        """
        refuse_reserved_delete(name)
        if not self._orgs.exists(name):
            raise KeyError(name)
        with self._marked_for_delete(name):
            # deleting() checked bindings with the mark up, and the mark refuses every bind since.
            if name not in self._held_deletes:
                self._refuse_bindings(name)
            for credential in self.ingest_credentials.owned_by(name):
                self.ingest_credentials.delete(credential.name)
            if not self._orgs.delete(name):
                raise KeyError(name)
            self._release_unheld(name, {claim.key for claim in self._keys.owned_by(name)})
        self._changed(name)

    @contextlib.contextmanager
    def deleting(self, name: str) -> Iterator[None]:
        """Hold *name* against new bindings while its hunts are written and then its delete.

        A delete refused because a credential binds the org, or a write binding it is in
        flight, is refused here, before it has written anything.

        Raises:
            KeyError: No org has the name.
            ReservedOrgError: It is the default org.
            OrgBoundError: A credential binds it, or a write binding it is in flight.
        """
        refuse_reserved_delete(name)
        if not self._orgs.exists(name):
            raise KeyError(name)
        with self._marked_for_delete(name):
            self._refuse_bindings(name)
            self._held_deletes.add(name)
            try:
                yield
            finally:
                self._held_deletes.discard(name)

    def bind_orgs(self, credential: IngestCredential) -> builtins.list[Claim]:
        """Claim each org *credential* binds, its owner among them, and return the claims made.

        Raises:
            OrgUnavailableError: One of them does not exist or is being deleted. The
                claims made are withdrawn.
        """
        made: builtins.list[Claim] = []
        try:
            for org in sorted({credential.org, *credential.orgs}):
                key = f"{org}/{credential.name}"
                claim = self._bindings.create(key, credential.name, subject=org)
                if claim is None:
                    self._bindings.claim(key, credential.name, subject=org)
                else:
                    made.append(claim)
                # After the claim: an earlier delete shows here, a later one sees the claim.
                if not self._orgs.exists(org) or self._being_deleted(org):
                    raise OrgUnavailableError(f"Org '{org}' does not exist or is being deleted")
        except BaseException:
            _withdraw(self._bindings, made)
            raise
        return made

    def withdraw_bindings(self, made: Iterable[Claim]) -> None:
        """Take back the binding claims a credential write that did not land made."""
        _withdraw(self._bindings, made)

    def settle_bindings(self, name: str, bound: Iterable[str], *, removed: bool = False) -> None:
        """Release credential *name*'s binding claims on orgs outside *bound*.

        Past the grace, so a write binding one again keeps it. A removed credential's
        claims go as they read: its record no longer binds anything.
        """
        kept = set(bound)
        for claim in self._bindings.owned_by(name):
            if removed:
                _withdraw(self._bindings, [claim])
            else:
                self._bindings.release_if_orphaned(
                    claim, held=claim.subject in kept, grace_seconds=self._grace
                )

    def release_orphaned_claims(self) -> builtins.list[str]:
        """Release every claim no record holds that is older than the grace.

        Covers name and tenant id claims, binding claims, and the locks and marks a
        create or delete takes.

        Returns:
            The keys released.
        """
        held_by: builtins.list[tuple[Claims, Callable[[Claim], bool]]] = [
            (self._keys, self._holds),
            (self._bindings, self._binds),
            (self._creates, lambda _claim: False),
            (self._deletes, lambda _claim: False),
        ]
        return [
            claim.key
            for claims, held in held_by
            for claim in claims.list()
            if claims.release_if_orphaned(claim, held=held(claim), grace_seconds=self._grace)
        ]

    @contextlib.contextmanager
    def _marked_for_delete(self, name: str) -> Iterator[None]:
        """Mark *name* as being deleted for as long as the block runs.

        Every delete of one org shares the mark, so a delete held by :meth:`deleting`
        takes it again inside without refusing itself.
        """
        self._deletes.claim(name, name)
        mark = self._deletes.get(name)
        try:
            yield
        finally:
            if mark is not None:
                _withdraw(self._deletes, [mark])

    def _refuse_bindings(self, name: str) -> None:
        """Refuse to delete *name* while a credential binds it or a write binding it is in flight.

        A credential *name* owns goes with the delete. A binding claim no credential holds
        and older than the grace is a write that never finished, and is released rather
        than counted.

        Raises:
            OrgBoundError: Naming each credential.
        """
        refuse_bound_delete(name, self.ingest_credentials)
        pending = sorted(
            claim.owner
            for claim in self._bindings.about(name)
            if self._binding_pending(claim, name)
        )
        if pending:
            raise OrgBoundError(
                f"Org '{name}' is being bound by ingest credential "
                f"{', '.join(repr(c) for c in pending)}: try again once that write finishes"
            )

    def _being_deleted(self, name: str) -> bool:
        """Whether a delete of org *name* is running. A mark older than the grace is a dead one."""
        mark = self._deletes.get(name)
        if mark is None:
            return False
        return not self._deletes.release_if_orphaned(mark, held=False, grace_seconds=self._grace)

    def _binds(self, claim: Claim) -> bool:
        """Whether the credential holding binding *claim* binds the org it is about."""
        credential = self.ingest_credentials.get(claim.owner)
        return credential is not None and claim.subject in credential.orgs

    def _binding_pending(self, claim: Claim, org: str) -> bool:
        """Whether binding *claim* on *org* holds off its delete. A credential *org* owns never does."""
        credential = self.ingest_credentials.get(claim.owner)
        if credential is not None and credential.org == org:
            return False
        held = credential is not None and claim.subject in credential.orgs
        return not self._bindings.release_if_orphaned(claim, held=held, grace_seconds=self._grace)

    def _lock_create(self, name: str) -> Claim:
        """Lock org *name* against every other create held by :meth:`reserved`.

        Raises:
            OrgExistsError: Another create holds the name.
        """
        for _ in range(DEFAULT_ATTEMPTS):
            lock = self._creates.create(name, uuid.uuid4().hex)
            if lock is not None:
                return lock
            held = self._creates.get(name)
            if held is None:
                continue  # released since the create found it
            if not self._creates.release_if_orphaned(held, held=False, grace_seconds=self._grace):
                raise OrgExistsError(f"Org '{name}' is being created by another request")
        raise DocumentConflictError(f"the create lock on org '{name}' changed hands every attempt")

    def _claim(self, owner: str, markers: Iterable[str]) -> builtins.list[Claim]:
        """Claim every marker for org *owner*, and return the claims this call made.

        A marker *owner* already holds is renewed and not returned, so undoing a failed
        write never takes away a claim an earlier write made.

        Raises:
            OrgCollisionError: Another org holds a marker. Every claim made that no
                stored org holds is withdrawn.
        """
        made: builtins.list[Claim] = []
        try:
            for marker in sorted(markers):
                claim = self._take(marker, owner)
                if claim is not None:
                    made.append(claim)
        except BaseException:
            self._withdraw_unheld(made)
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

    def _withdraw_unheld(self, made: Iterable[Claim]) -> None:
        """Withdraw each claim a failed write made that no stored org holds.

        A write can land and still raise, so a claim its record holds stays. One whose
        record cannot be read stays too, until the grace releases it.
        """
        for claim in made:
            try:
                if not self._holds(claim):
                    self._keys.withdraw(claim)
            except Exception as exc:
                logger.warning("claim not withdrawn", key=claim.key, error=str(exc))

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


def _withdraw(claims: Claims, made: Iterable[Claim]) -> None:
    """Withdraw claims a write made. One that will not go is left to the grace."""
    for claim in made:
        try:
            claims.withdraw(claim)
        except Exception as exc:
            logger.warning("claim not withdrawn", key=claim.key, error=str(exc))


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
