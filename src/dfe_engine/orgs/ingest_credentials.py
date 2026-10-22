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

Records are YAML files, ``{dir}/{id}.yaml``, with the id as the filename stem. The
token itself is never stored here: it lives in the secrets backend
(:mod:`dfe_engine.orgs.ingest_tokens`).
"""

import builtins
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

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

    def put(self, credential: IngestCredential) -> None:
        """Create or replace *credential*.

        Raises:
            CredentialIdTakenError: Another org owns a credential with this id.
        """
        held = self.get(credential.name)
        if held is not None and held.org != credential.org:
            raise CredentialIdTakenError(
                f"Ingest credential id '{credential.name}' is taken; ids are unique across "
                "every org because the receiver keys its bindings on them"
            )
        yaml_dump(credential.model_dump(mode="json", exclude={"name"}), self._path(credential))
        self._changed(credential.org)

    def delete(self, name: str) -> None:
        """Remove the credential named *name*.

        Raises:
            KeyError: No credential has that id.
        """
        held = self.get(name)
        if held is None:
            raise KeyError(name)
        self._path(held).unlink()
        self._changed(held.org)
        self._removed(held.name)

    def _path(self, credential: IngestCredential) -> Path:
        return self._dir / f"{credential.name}.yaml"

    def _read(self, path: Path) -> IngestCredential:
        data: dict = yaml_load(path)
        data["name"] = path.stem
        return IngestCredential.model_validate(data)
