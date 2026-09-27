#  Project:      dfe-engine
#  File:         groups.py
#  Purpose:      YAML-backed group store for managing groups with role mappings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from scalo.logger import logger

from dfe_engine.auth.protected_accounts import resolve_floor
from dfe_engine.auth.store_names import VALID_NAME, store_key
from dfe_engine.orgs.models import ORG_NAME_PATTERN
from dfe_engine.yaml_utils import yaml_dump, yaml_load

if TYPE_CHECKING:
    from dfe_engine.store.documents import DocuStore

GROUP_SCOPE_SYSTEM = "system"
_ORG_SCOPE_PREFIX = "org:"


def validate_group_scope(scope: str) -> str:
    """Validate a group scope string: ``system`` or ``org:<name>``.

    ``<name>`` follows the org name rule, since it is looked up in the org registry.

    Returns the scope unchanged; raises ValueError otherwise.
    """
    if scope == GROUP_SCOPE_SYSTEM:
        return scope
    org = scope.removeprefix(_ORG_SCOPE_PREFIX)
    if org != scope and re.fullmatch(ORG_NAME_PATTERN, org):
        return scope
    raise ValueError(f"Group scope must be 'system' or 'org:<name>', got '{scope}'")


class Group(BaseModel):
    """A named group with roles and member usernames."""

    name: str
    description: str = ""
    roles: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    scope: str = GROUP_SCOPE_SYSTEM
    """Where this group lives: ``system`` (spans orgs, today's default) or
    ``org:<name>`` (exists only inside that org - invisible outside it, and its
    roles bind at that org's scope only, never system-wide)."""
    source_provider: str = ""
    """Name of the OIDC provider that owns this group (empty for manually managed groups)."""
    source_id: str = ""
    """Provider-specific group identifier (e.g. Google group key, Entra object ID)."""
    org_ids: list[str] = Field(default_factory=list)
    """Organisation IDs this group has access to (empty means no org-scoped access)."""
    attributes: dict[str, Any] = Field(default_factory=dict)
    """Free-form NON-sensitive attributes (nested JSON, KVP is the floor).

    Stored inline on the group and round-trips through both backends. Never put
    secrets here - a broad group read returns this blob; sensitive attributes live
    in the separate keyed store (:mod:`dfe_engine.auth.attributes`)."""

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, value: str) -> str:
        return validate_group_scope(value)

    @property
    def scope_org(self) -> str:
        """Owning org name for org-scoped groups, '' for system groups."""
        if self.scope.startswith(_ORG_SCOPE_PREFIX):
            return self.scope[len(_ORG_SCOPE_PREFIX) :]
        return ""


class GroupStore:
    """YAML-backed store for managing groups with role mappings.

    Each group is persisted as ``{name}.yaml`` under the groups directory.
    The group name is the filename stem -- it is NOT stored inside the YAML file.

    Example layout::

        groups/
            admins.yaml     # { description: ..., roles: [...], members: [...] }
            operators.yaml

    Attributes:
        protected: The recovery credentials this store refuses to drop from the
            admin-role group (:mod:`dfe_engine.auth.protected_accounts`). Public
            so a caller can refuse a batch removal before applying any of it.
    """

    def __init__(self, groups_dir: Path, *, admin_name: str = "") -> None:
        self._dir = Path(groups_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.protected = resolve_floor(admin_name)
        self._skipped: set[str] = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, name: str) -> Path:
        """The file group *name* lives in.

        Raises:
            KeyError: No group can hold *name*, so it is looked up nowhere.
        """
        return self._dir / f"{store_key(name)}.yaml"

    def _read(self, name: str) -> Group | None:
        try:
            path = self._path(name)
        except KeyError:
            return None
        return self._load(path)

    def _load(self, path: Path) -> Group | None:
        """The group in *path*, or None when there is none or it is not a valid group.

        An invalid file is skipped, not raised, so it cannot take down role resolution
        for every session. Its members lose that group's roles until it is fixed.
        """
        if not path.exists():
            return None
        data = yaml_load(path)
        if data is None:
            data = {}
        # Name is derived from filename, not stored in the file
        data["name"] = path.stem
        try:
            return Group.model_validate(data)
        except ValidationError as exc:
            # Role resolution lists every group per request, so warn once per file.
            if path.name not in self._skipped:
                self._skipped.add(path.name)
                logger.warning(
                    "group file skipped: not a valid group", path=str(path), error=str(exc)
                )
            return None

    def _write(self, group: Group) -> None:
        # Exclude the name field -- it lives in the filename, not the YAML
        data = group.model_dump(exclude={"name"})
        yaml_dump(data, self._path(group.name))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        roles: list[str],
        description: str = "",
        *,
        members: list[str] | None = None,
        scope: str = GROUP_SCOPE_SYSTEM,
    ) -> Group:
        """Create a new group and persist it to YAML.

        Raises:
            ValueError: If a group with this name already exists, or the
                scope is not ``system`` / ``org:<name>``.
        """
        if not VALID_NAME.match(name):
            raise ValueError(f"Invalid group name: {name!r}")
        if self._path(name).exists():
            raise ValueError(f"Group '{name}' already exists")
        member_list: list[str] = []
        if members:
            seen: set[str] = set()
            for username in members:
                if username not in seen:
                    seen.add(username)
                    member_list.append(username)
        group = Group(
            name=name,
            roles=roles,
            description=description,
            members=member_list,
            scope=scope,
        )
        self._write(group)
        return group

    def get(self, name: str) -> Group | None:
        """Return the named group, or None if it does not exist."""
        return self._read(name)

    def list(self) -> list[Group]:
        """Return all groups sorted by name."""
        groups = []
        for path in sorted(self._dir.glob("*.yaml")):
            group = self._load(path)
            if group is not None:
                groups.append(group)
        return groups

    def by_source_id(self) -> dict[str, Group]:
        """Return groups keyed by their provider ``source_id``.

        Only groups that carry a non-empty ``source_id`` appear. Used to resolve
        a token that carries opaque provider identifiers (Entra object GUIDs,
        Google group keys) rather than group names - the sync stores the id on
        the group file, and this is how login looks it back up.

        When two groups share a source_id (a misconfiguration) the
        last-by-sorted-name wins; that is deterministic rather than correct, and
        such a collision is a config error worth avoiding.
        """
        index: dict[str, Group] = {}
        for group in self.list():
            if group.source_id:
                index[group.source_id] = group
        return index

    def update(self, name: str, *, allow_protected: bool = False, **fields: object) -> Group:
        """Update one or more fields on an existing group and persist.

        Accepted keyword arguments: ``roles``, ``description``, ``members``,
        ``org_ids``, ``scope``.

        Args:
            name: Group to update.
            allow_protected: Replace the admin-role group's members with a list
                that drops a recovery credential. Reserved for the reconcile paths.
            **fields: Fields to update.

        Returns:
            The updated Group.

        Raises:
            KeyError: If the group does not exist.
            ValueError: If ``scope`` is not ``system`` / ``org:<name>``.
            ProtectedAccountError: The replacement drops a recovery credential
                from the admin-role group.
        """
        group = self._read(name)
        if group is None:
            raise KeyError(f"Group '{name}' not found")
        if "scope" in fields:
            # model_copy(update=...) skips validators - check explicitly.
            validate_group_scope(str(fields["scope"]))
        if not allow_protected and "members" in fields:
            self.protected.check_members_replaced(name, group.members, fields["members"])
        updated = group.model_copy(update=fields)
        self._write(updated)
        return updated

    def set_attributes(self, name: str, attributes: dict) -> Group:
        """Full-replace the non-sensitive ``attributes`` blob on a group.

        Args:
            name: Group to update.
            attributes: The new attributes dict (replaces the field wholesale).

        Returns:
            The updated Group.

        Raises:
            KeyError: If the group does not exist.
        """
        group = self._read(name)
        if group is None:
            raise KeyError(f"Group '{name}' not found")
        updated = group.model_copy(update={"attributes": attributes})
        self._write(updated)
        return updated

    def delete(self, name: str) -> None:
        """Delete the named group.

        Raises:
            KeyError: If the group does not exist.
            ValueError: If the group still has members.
        """
        group = self._read(name)
        if group is None:
            raise KeyError(f"Group '{name}' not found")
        if group.members:
            raise ValueError(
                f"Cannot delete group '{name}': remove all {len(group.members)} member(s) first"
            )
        self._path(name).unlink()

    def add_member(self, group_name: str, username: str) -> None:
        """Add a username to the group's member list (idempotent).

        Raises:
            KeyError: If the group does not exist.
        """
        group = self._read(group_name)
        if group is None:
            raise KeyError(f"Group '{group_name}' not found")
        if username not in group.members:
            updated = group.model_copy(update={"members": [*group.members, username]})
            self._write(updated)

    def remove_member(
        self, group_name: str, username: str, *, allow_protected: bool = False
    ) -> None:
        """Remove a username from the group's member list.

        Removing a user who is not in the group is a no-op.

        Args:
            group_name: Group to change.
            username: Member to remove.
            allow_protected: Remove a recovery credential from the admin-role
                group. Reserved for the reconcile paths.

        Raises:
            KeyError: If the group does not exist.
            ProtectedAccountError: *username* is a recovery credential and
                *group_name* is the group it holds the admin role through.
        """
        group = self._read(group_name)
        if group is None:
            raise KeyError(f"Group '{group_name}' not found")
        if not allow_protected:
            self.protected.check_member_removal(group_name, [username])
        updated_members = [m for m in group.members if m != username]
        if updated_members != group.members:
            updated = group.model_copy(update={"members": updated_members})
            self._write(updated)

    def resolve_roles_for_member(self, username: str) -> list[str]:
        """Return the sorted unique list of roles held by a member across all groups.

        Scans every group file; a user accumulates roles from all groups they
        belong to.  Returns an empty list if the user is in no groups.
        """
        roles: set[str] = set()
        for group in self.list():
            if username in group.members:
                roles.update(group.roles)
        return sorted(roles)


# Fields update() may change; the name is the document key and never moves,
# and source_provider / source_id are owned by the sync path. Kept identical to
# the documented GroupStore.update contract.
_UPDATABLE_GROUP_FIELDS = (
    "roles",
    "description",
    "members",
    "org_ids",
    "scope",
)


class DocuStoreGroupStore:
    """Document-store-backed group store - the same interface as :class:`GroupStore`.

    Persists one :class:`Group` document per name (keyed and unique-indexed on
    ``name``) instead of one YAML file. Every domain rule - name validation,
    member de-duplication, scope validation, the protected-name floor, and the
    delete-with-members guard - is identical to the YAML store, so the two are
    drop-in interchangeable behind the same construction seam.

    Attributes:
        protected: The recovery credentials this store refuses to drop from the
            admin-role group.
    """

    def __init__(
        self,
        store: DocuStore,
        *,
        collection: str = "groups",
        admin_name: str = "",
    ) -> None:
        self._c = store.typed(collection, Group, key="name")
        self.protected = resolve_floor(admin_name)

    def create(
        self,
        name: str,
        roles: list[str],
        description: str = "",
        *,
        members: list[str] | None = None,
        scope: str = GROUP_SCOPE_SYSTEM,
    ) -> Group:
        """Create a new group. Raises ValueError if the name is invalid/taken or scope is bad."""
        if not VALID_NAME.match(name):
            raise ValueError(f"Invalid group name: {name!r}")
        if self._c.exists(name):
            raise ValueError(f"Group '{name}' already exists")
        member_list: list[str] = []
        if members:
            seen: set[str] = set()
            for username in members:
                if username not in seen:
                    seen.add(username)
                    member_list.append(username)
        group = Group(
            name=name,
            roles=roles,
            description=description,
            members=member_list,
            scope=scope,
        )
        self._c.put(name, group)
        return group

    def get(self, name: str) -> Group | None:
        """Return the named group, or None if it does not exist."""
        try:
            return self._c.get(store_key(name))
        except KeyError:
            return None

    def list(self) -> list[Group]:
        """Return all groups sorted by name."""
        return self._c.list()

    def by_source_id(self) -> dict[str, Group]:
        """Return groups keyed by their provider ``source_id`` (only non-empty ones).

        When two groups share a source_id (a misconfiguration) the
        last-by-sorted-name wins - deterministic rather than correct, and such a
        collision is a config error worth avoiding.
        """
        index: dict[str, Group] = {}
        for group in self.list():
            if group.source_id:
                index[group.source_id] = group
        return index

    def update(self, name: str, *, allow_protected: bool = False, **fields: object) -> Group:
        """Update permitted fields on a group. Raises KeyError if missing, ValueError on bad scope.

        Accepted keyword arguments: ``roles``, ``description``, ``members``,
        ``org_ids``, ``scope``. ``allow_protected`` replaces the admin-role
        group's members with a list that drops a recovery credential; reserved for
        the reconcile paths. Raises ProtectedAccountError otherwise.
        """
        group = self.get(name)
        if group is None:
            raise KeyError(f"Group '{name}' not found")
        if "scope" in fields:
            # model_copy(update=...) skips validators - check explicitly.
            validate_group_scope(str(fields["scope"]))
        if not allow_protected and "members" in fields:
            self.protected.check_members_replaced(name, group.members, fields["members"])
        updates = {k: fields[k] for k in _UPDATABLE_GROUP_FIELDS if k in fields}
        updated = group.model_copy(update=updates)
        self._c.put(name, updated)
        return updated

    def set_attributes(self, name: str, attributes: dict) -> Group:
        """Full-replace the non-sensitive ``attributes`` blob. Raises KeyError if missing."""
        group = self.get(name)
        if group is None:
            raise KeyError(f"Group '{name}' not found")
        updated = group.model_copy(update={"attributes": attributes})
        self._c.put(name, updated)
        return updated

    def delete(self, name: str) -> None:
        """Delete the named group. Raises KeyError if missing, ValueError if it has members."""
        group = self.get(name)
        if group is None:
            raise KeyError(f"Group '{name}' not found")
        if group.members:
            raise ValueError(
                f"Cannot delete group '{name}': remove all {len(group.members)} member(s) first"
            )
        self._c.delete(name)

    def add_member(self, group_name: str, username: str) -> None:
        """Add a username to the group's member list (idempotent). Raises KeyError if missing."""
        group = self.get(group_name)
        if group is None:
            raise KeyError(f"Group '{group_name}' not found")
        if username not in group.members:
            updated = group.model_copy(update={"members": [*group.members, username]})
            self._c.put(group_name, updated)

    def remove_member(
        self, group_name: str, username: str, *, allow_protected: bool = False
    ) -> None:
        """Remove a username from the group's member list (no-op if absent). KeyError if missing.

        ``allow_protected`` removes a recovery credential from the admin-role
        group; reserved for the reconcile paths. Raises ProtectedAccountError
        otherwise.
        """
        group = self.get(group_name)
        if group is None:
            raise KeyError(f"Group '{group_name}' not found")
        if not allow_protected:
            self.protected.check_member_removal(group_name, [username])
        updated_members = [m for m in group.members if m != username]
        if updated_members != group.members:
            updated = group.model_copy(update={"members": updated_members})
            self._c.put(group_name, updated)

    def resolve_roles_for_member(self, username: str) -> list[str]:
        """Return the sorted unique list of roles held by a member across all groups."""
        roles: set[str] = set()
        for group in self.list():
            if username in group.members:
                roles.update(group.roles)
        return sorted(roles)
