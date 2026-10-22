#  Project:      dfe-engine
#  File:         orgs/ingest_credentials.py
#  Purpose:      Ingest credentials an org owns, and the orgs each one's records may land in
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Ingest credentials: the bearer tokens senders present to dfe-receiver.

Each credential is owned by one org and binds the orgs its records may land in.
dfe-receiver reads the binding as ``ingest_bindings.credentials.<id>`` and holds a
record's ``_org_id`` to it, so a record lands only in an org its credential is
bound to. The id is the receiver's key for the binding and the ``<id>:`` before
each token in its bearer secret, which is why ids are unique across every org.

Records are YAML files, ``{dir}/{id}.yaml``, with the id as the filename stem, or
one document each in the document store where engine replicas share them. The
token itself is never stored here: it lives in the secrets backend
(:mod:`dfe_engine.orgs.ingest_tokens`).
"""

import builtins
import functools
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field

from dfe_engine.store.documents import (
    DEFAULT_ATTEMPTS,
    Claim,
    DocumentConflictError,
    DocuStore,
    StaleRevisionError,
)
from dfe_engine.yaml_utils import yaml_dump, yaml_load

# dfe-receiver's is_credential_id: a ':' would split a token entry.
CREDENTIAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"

IngestListener = Literal["http", "grpc", "otlp", "splunk_hec", "prometheus_rw"]
"""A dfe-receiver listener that authenticates a bearer token, by its transport label."""

BEARER_LISTENERS: tuple[IngestListener, ...] = get_args(IngestListener)

LISTENER_SECTIONS: dict[IngestListener, str] = {
    "http": "server",
    "grpc": "grpc",
    "otlp": "otlp",
    "splunk_hec": "splunk_hec",
    "prometheus_rw": "prometheus_rw",
}
"""The dfe-receiver config section whose ``auth`` block authenticates each listener."""


class IngestCredential(BaseModel):
    """One ingest credential, as it is stored.

    Attributes:
        name: The credential id, unique across every org.
        org: The org that owns it. It is always one of ``orgs``.
        orgs: The orgs its records may land in, by org name.
        default_org: The org its records take when no rule picks one of ``orgs``.
        listeners: The receiver listeners that accept its token.
        created_at: ISO 8601 time of the first mint.
        rotated_at: ISO 8601 time of the last mint.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=CREDENTIAL_ID_PATTERN)
    org: str
    orgs: list[str] = Field(min_length=1)
    default_org: str
    listeners: list[IngestListener] = Field(min_length=1)
    created_at: str = ""
    rotated_at: str = ""

    def content(self) -> dict[str, object]:
        """The fields an operator sets, which the credential's ETag is taken over."""
        return self.model_dump(mode="json", exclude={"created_at", "rotated_at"})


class CredentialIdTakenError(ValueError):
    """Another org already holds a credential with this id."""


class CredentialChangedError(ValueError):
    """The stored credential is not the one a conditional write was made against."""


class OrgUnavailableError(ValueError):
    """An org a credential write binds does not exist or is being deleted."""


class OrgBinder(Protocol):
    """Claims the orgs a credential binds, so none is deleted while a write binding it is made."""

    def bind_orgs(self, credential: IngestCredential) -> builtins.list[Claim]:
        """Claim each org *credential* binds, its owner among them, and return the claims made.

        Raises:
            OrgUnavailableError: One of them does not exist or is being deleted.
        """
        ...

    def withdraw_bindings(self, made: Iterable[Claim]) -> None:
        """Take back the claims a write that did not land made."""
        ...

    def settle_bindings(self, name: str, bound: Iterable[str], *, removed: bool = False) -> None:
        """Release credential *name*'s claims on orgs outside *bound*.

        A claim is released once it is older than the grace, unless the credential was
        *removed*: a delete takes its claims back as they read.
        """
        ...


_ID_TAKEN = (
    "Ingest credential id '{name}' is taken; ids are unique across every org because the "
    "receiver keys its bindings on them"
)


def _checked_condition(*, new: bool, current: IngestCredential | None) -> None:
    """Raises: ValueError: A write was conditioned on both being new and on a stored credential."""
    if new and current is not None:
        raise ValueError("a credential write is conditioned on new or on current, never both")


class IngestCredentialRegistry:
    """YAML-backed store for ingest credentials, one ``{id}.yaml`` per credential.

    Built by :class:`~dfe_engine.orgs.registry.OrgRegistry`, whose change listeners
    it calls after every write, so a listener sees credential and org writes alike.
    ``removed`` is called with each deleted credential's id, after ``changed``.
    """

    def __init__(
        self,
        directory: Path,
        changed: Callable[[str], None],
        removed: Callable[[str], None],
    ) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._changed = changed
        self._removed = removed

    def get(self, name: str) -> IngestCredential | None:
        """The credential named *name*, or None when there is none."""
        if re.fullmatch(CREDENTIAL_ID_PATTERN, name) is None:
            return None
        path = self._dir / f"{name}.yaml"
        if not path.exists():
            return None
        return self._read(path)

    def list(self) -> builtins.list[IngestCredential]:
        """Every credential, sorted by id."""
        return [self._read(p) for p in sorted(self._dir.glob("*.yaml"))]

    def owned_by(self, org: str) -> builtins.list[IngestCredential]:
        """The credentials *org* owns, sorted by id."""
        return [c for c in self.list() if c.org == org]

    def binding(self, org: str) -> builtins.list[IngestCredential]:
        """The credentials another org owns that bind *org*, sorted by id."""
        return [c for c in self.list() if c.org != org and org in c.orgs]

    def put(
        self,
        credential: IngestCredential,
        *,
        new: bool = False,
        current: IngestCredential | None = None,
    ) -> None:
        """Create or replace *credential*.

        Args:
            credential: The credential to store.
            new: Store it only where no credential has its id.
            current: Store it only where the stored credential is still this one.

        Raises:
            CredentialIdTakenError: Another org owns a credential with this id.
            CredentialChangedError: ``new`` and one is stored, or the stored one is
                not ``current``.
        """
        _checked_condition(new=new, current=current)
        held = self.get(credential.name)
        if held is not None and held.org != credential.org:
            raise CredentialIdTakenError(_ID_TAKEN.format(name=credential.name))
        if (new and held is not None) or (current is not None and held != current):
            raise CredentialChangedError(f"Ingest credential '{credential.name}' changed")
        yaml_dump(credential.model_dump(mode="json", exclude={"name"}), self._path(credential))
        self._changed(credential.org)

    def delete(self, name: str, *, current: IngestCredential | None = None) -> None:
        """Remove the credential named *name*.

        Args:
            name: The credential id.
            current: Remove it only where the stored credential is still this one.

        Raises:
            KeyError: No credential has that id.
            CredentialChangedError: The stored credential is not ``current``.
        """
        held = self.get(name)
        if held is None:
            raise KeyError(name)
        if current is not None and held != current:
            raise CredentialChangedError(f"Ingest credential '{name}' changed")
        self._path(held).unlink()
        self._changed(held.org)
        self._removed(held.name)

    def _path(self, credential: IngestCredential) -> Path:
        return self._dir / f"{credential.name}.yaml"

    def _read(self, path: Path) -> IngestCredential:
        data: dict = yaml_load(path)
        data["name"] = path.stem
        return IngestCredential.model_validate(data)


class DocuStoreIngestCredentialRegistry:
    """Ingest credentials in the document store, one document per credential, keyed on its id.

    The interface of :class:`IngestCredentialRegistry`, built by
    :class:`~dfe_engine.orgs.registry.DocuStoreOrgRegistry`. The unique index on the id
    decides a create, and a conditional write lands only on the document and revision the
    stored credential was read at, so replicas racing one id never both land, and a write
    never lands on one deleted and made again since. *binder* claims
    each org a write binds before the write, so a delete of that org running at the same
    time refuses or is refused.
    """

    def __init__(
        self,
        store: DocuStore,
        changed: Callable[[str], None],
        removed: Callable[[str], None],
        *,
        binder: OrgBinder,
        collection: str = "ingest_credentials",
    ) -> None:
        self._c = store.typed(collection, IngestCredential, key="name")
        self._changed = changed
        self._removed = removed
        self._binder = binder

    def get(self, name: str) -> IngestCredential | None:
        """The credential named *name*, or None when there is none."""
        if re.fullmatch(CREDENTIAL_ID_PATTERN, name) is None:
            return None
        return self._c.get(name)

    def list(self) -> builtins.list[IngestCredential]:
        """Every credential, sorted by id."""
        return self._c.list()

    def owned_by(self, org: str) -> builtins.list[IngestCredential]:
        """The credentials *org* owns, sorted by id."""
        return self._c.list_by("org", org)

    def binding(self, org: str) -> builtins.list[IngestCredential]:
        """The credentials another org owns that bind *org*, sorted by id."""
        return [c for c in self.list() if c.org != org and org in c.orgs]

    def put(
        self,
        credential: IngestCredential,
        *,
        new: bool = False,
        current: IngestCredential | None = None,
    ) -> None:
        """Create or replace *credential*, as :meth:`IngestCredentialRegistry.put` does.

        Raises:
            CredentialIdTakenError: Another org owns a credential with this id.
            CredentialChangedError: ``new`` and one is stored, or the stored one is
                not ``current``.
            OrgUnavailableError: An org it binds does not exist or is being deleted.
            DocumentConflictError: Concurrent writers beat every attempt.
        """
        _checked_condition(new=new, current=current)
        made = self._binder.bind_orgs(credential)
        try:
            if current is not None:
                self._replace(credential, current)
            elif new:
                self._create(credential)
            else:
                self._upsert(credential)
        except BaseException:
            self._binder.withdraw_bindings(made)
            raise
        self._binder.settle_bindings(credential.name, credential.orgs)
        self._changed(credential.org)

    def delete(self, name: str, *, current: IngestCredential | None = None) -> None:
        """Remove the credential named *name*, as :meth:`IngestCredentialRegistry.delete` does.

        Raises:
            KeyError: No credential has that id.
            CredentialChangedError: The stored credential is not ``current``.
            DocumentConflictError: Concurrent writers beat every attempt.
        """
        valid = re.fullmatch(CREDENTIAL_ID_PATTERN, name) is not None
        found = self._c.get_with_revision(name) if valid else None
        if found is None:
            raise KeyError(name)
        held, rev = found
        if current is not None and held != current:
            raise CredentialChangedError(f"Ingest credential '{name}' changed")
        if not self._c.delete(name, expected_rev=None if current is None else rev):
            if current is not None and self._c.exists(name):
                raise CredentialChangedError(f"Ingest credential '{name}' changed")
            raise KeyError(name)
        self._binder.settle_bindings(name, (), removed=True)
        self._changed(held.org)
        self._removed(held.name)

    def _create(self, credential: IngestCredential) -> None:
        if self._c.insert(credential):
            return
        held = self._c.get(credential.name)
        if held is not None and held.org != credential.org:
            raise CredentialIdTakenError(_ID_TAKEN.format(name=credential.name))
        raise CredentialChangedError(f"Ingest credential '{credential.name}' already exists")

    def _replace(self, credential: IngestCredential, current: IngestCredential) -> None:
        found = self._c.get_with_revision(credential.name)
        if found is not None and found[0].org != credential.org:
            raise CredentialIdTakenError(_ID_TAKEN.format(name=credential.name))
        if found is None or found[0] != current:
            raise CredentialChangedError(f"Ingest credential '{credential.name}' changed")
        try:
            self._c.mutate(credential.name, lambda _: credential, expected_rev=found[1])
        except (KeyError, StaleRevisionError) as exc:
            raise CredentialChangedError(f"Ingest credential '{credential.name}' changed") from exc

    def _upsert(self, credential: IngestCredential) -> None:
        for _ in range(DEFAULT_ATTEMPTS):
            if self._c.insert(credential):
                return
            try:
                self._c.mutate(credential.name, functools.partial(_same_owner, credential))
            except KeyError:
                continue  # removed since the insert found it: insert again
            return
        raise DocumentConflictError(
            f"ingest credential '{credential.name}' was created and removed under every attempt"
        )


def _same_owner(credential: IngestCredential, stored: IngestCredential) -> IngestCredential:
    """*credential* in place of *stored*, which must have the same owning org.

    Raises:
        CredentialIdTakenError: Another org owns *stored*.
    """
    if stored.org != credential.org:
        raise CredentialIdTakenError(_ID_TAKEN.format(name=credential.name))
    return credential
