#  Project:      dfe-engine
#  File:         auth/attributes.py
#  Purpose:      Separate keyed store for SENSITIVE user/group attributes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Keyed store for SENSITIVE user/group attributes, kept out of the entity models.

Non-sensitive attributes live inline on :class:`~dfe_engine.auth.accounts.Account`
and :class:`~dfe_engine.auth.groups.Group`, so a broad account/group read returns
them. Sensitive attributes must NEVER ride inline for exactly that reason -- a wide
read would leak them. They live here instead: one plain dict per entity id, in a
SEPARATE store that is only ever read deliberately.

Two backends mirror the account-store split -- :class:`AttributeStore` (one YAML
file per entity) and :class:`DocuStoreAttributeStore` (one document per entity) --
so the two are drop-in interchangeable behind the same construction seam. Both are
generic over the entity kind: instantiate one per kind later (e.g. collections
``sensitive_account_attributes`` / ``sensitive_group_attributes``).

Usage::

    from pathlib import Path
    from dfe_engine.auth.attributes import AttributeStore

    store = AttributeStore(Path("/etc/dfe/sensitive/accounts"))
    store.put("alice", {"otp_seed": "..."})
    assert store.get("alice") == {"otp_seed": "..."}
    store.delete("alice")
"""

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from dfe_engine.auth.store_names import VALID_NAME
from dfe_engine.yaml_utils import yaml_dump, yaml_load

if TYPE_CHECKING:
    from dfe_engine.store.documents import DocuStore

MAX_ATTRIBUTE_DEPTH = 64
"""Deepest an attributes blob may nest; the YAML writer recurses once per level."""


def check_attribute_depth(attributes: dict) -> dict:
    """Return *attributes* unchanged, refusing a blob nested past MAX_ATTRIBUTE_DEPTH.

    The top-level mapping is level one. The walk is iterative, so a blob nested far
    past the cap is measured without recursing through it.

    Raises:
        ValueError: The blob nests deeper than MAX_ATTRIBUTE_DEPTH.
    """
    pending: list[tuple[object, int]] = [(attributes, 1)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_ATTRIBUTE_DEPTH:
            raise ValueError(f"attributes may nest at most {MAX_ATTRIBUTE_DEPTH} levels deep")
        if isinstance(value, dict):
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            continue
        pending.extend((child, depth + 1) for child in children if isinstance(child, dict | list))
    return attributes


class AttributeStore:
    """YAML-backed sensitive-attribute store, keyed by entity id.

    Each entity's sensitive attributes are a plain dict persisted as
    ``{entity_id}.yaml`` under the store directory. Absent file means no
    attributes, and reads return an empty dict rather than raising.
    """

    def __init__(self, attributes_dir: Path) -> None:
        self._dir = Path(attributes_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(self, entity_id: str) -> dict:
        """Return the sensitive attributes for *entity_id*, or an empty dict if absent.

        Args:
            entity_id: The account / group id to look up.

        Returns:
            The stored attributes dict, or an empty dict when nothing is stored.

        Raises:
            ValueError: If *entity_id* is not a safe name.
        """
        path = self._path(entity_id)
        if not path.exists():
            return {}
        data = yaml_load(path)
        return data if isinstance(data, dict) else {}

    def put(self, entity_id: str, attributes: dict) -> None:
        """Full-replace the sensitive attributes for *entity_id*.

        Args:
            entity_id: The account / group id to write.
            attributes: The attributes dict (replaces any stored value wholesale).

        Raises:
            ValueError: If *entity_id* is not a safe name.
        """
        yaml_dump(dict(attributes), self._path(entity_id))

    def delete(self, entity_id: str) -> None:
        """Remove the stored attributes for *entity_id* (no-op if absent).

        Args:
            entity_id: The account / group id to clear.

        Raises:
            ValueError: If *entity_id* is not a safe name.
        """
        path = self._path(entity_id)
        if path.exists():
            path.unlink()

    def _path(self, entity_id: str) -> Path:
        # The id is an account or group name, so it follows their rule.
        if not VALID_NAME.match(entity_id):
            raise ValueError(f"Invalid entity id: {entity_id!r}")
        return self._dir / f"{entity_id}.yaml"


class _EntityAttributes(BaseModel):
    """One entity's sensitive attributes, keyed on ``entity_id`` in the document store."""

    entity_id: str
    attributes: dict = Field(default_factory=dict)


class DocuStoreAttributeStore:
    """Document-store-backed sensitive-attribute store - the same interface as :class:`AttributeStore`.

    Persists one :class:`_EntityAttributes` document per entity id (keyed and
    unique-indexed on ``entity_id``) instead of one YAML file. A missing document
    reads back as an empty dict, so the two backends are drop-in interchangeable.
    """

    def __init__(self, store: DocuStore, *, collection: str) -> None:
        self._c = store.typed(collection, _EntityAttributes, key="entity_id")

    def get(self, entity_id: str) -> dict:
        """Return the sensitive attributes for *entity_id*, or an empty dict if absent."""
        doc = self._c.get(entity_id)
        return doc.attributes if doc is not None else {}

    def put(self, entity_id: str, attributes: dict) -> None:
        """Full-replace the sensitive attributes for *entity_id*."""
        self._c.put(entity_id, _EntityAttributes(entity_id=entity_id, attributes=dict(attributes)))

    def delete(self, entity_id: str) -> None:
        """Remove the stored attributes for *entity_id* (no-op if absent)."""
        self._c.delete(entity_id)
