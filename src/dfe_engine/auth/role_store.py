#  Project:      dfe-engine
#  File:         role_store.py
#  Purpose:      YAML-backed CRUD for roles.yaml role definitions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CRUD for role definitions: one roles.yaml, or one document per custom role."""

import builtins
import functools
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from dfe_engine.auth.roles import (
    LiveRoleConfig,
    RoleConfig,
    RoleDefinition,
    RoleResourceType,
    builtin_core_role_names,
)
from dfe_engine.core_resources.yaml_resource_type import CORE_RESOURCE_MUTATION_MESSAGE
from dfe_engine.store.live import REFRESH_SECONDS
from dfe_engine.yaml_utils import yaml_dump

if TYPE_CHECKING:
    from dfe_engine.store.documents import DocuStore

# A binding claim no group holds, or a delete's mark, is a write that never finished once this old.
# The age compares this replica's clock with the writer's, so replicas need clocks synced by NTP.
BINDING_GRACE_SECONDS = 60.0

type Holders = Callable[[str], Iterable[str]]
"""Returns the names of the groups that hold a role, given its name."""


class RoleInUseError(ValueError):
    """A group holds the role, or is being given it, so it cannot be deleted."""


class RoleUnavailableError(ValueError):
    """A group was to be given a role that does not exist or is being deleted."""


class Role(BaseModel):
    """Named role with permission patterns."""

    name: str
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    scoped: bool = False
    resource_type: RoleResourceType = "custom"


class _StoredRole(Role):
    """A custom role as the document store holds it, marked while its delete runs.

    Attributes:
        deleting_since: When a delete marked it, in seconds since the epoch, 0 when unmarked.
    """

    deleting_since: float = 0.0


class RoleStore:
    """CRUD for the single ``roles.yaml`` file (``roles:`` map)."""

    # Annotations in this class name `builtins.list`, because the `list` method shadows the builtin here.

    def __init__(self, roles_path: Path) -> None:
        self._path = Path(roles_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """The roles file this store reads and writes."""
        return self._path

    def load_config(self) -> RoleConfig:
        """Load the roles file, raising FileNotFoundError when it is absent."""
        if not self._path.exists():
            raise FileNotFoundError(f"Roles file not found: {self._path}")
        return RoleConfig.load(self._path)

    def list(self) -> builtins.list[Role]:
        """Every role, in name order."""
        config = self.load_config()
        return [
            Role(name=name, **definition.model_dump())
            for name, definition in sorted(config.roles.items())
        ]

    def get(self, name: str) -> Role | None:
        """The named role, or None."""
        config = self.load_config()
        definition = config.roles.get(name)
        if definition is None:
            return None
        return Role(name=name, **definition.model_dump())

    def create(
        self,
        name: str,
        *,
        description: str,
        permissions: builtins.list[str],
        scoped: bool = False,
    ) -> Role:
        """Add a custom role, refusing a name already taken."""
        config = self.load_config()
        if name in config.roles:
            raise ValueError(f"Role '{name}' already exists")
        roles = dict(config.roles)
        roles[name] = RoleDefinition(
            description=description,
            permissions=permissions,
            scoped=scoped,
            resource_type="custom",
        )
        self._write_roles(roles)
        return Role(
            name=name,
            description=description,
            permissions=permissions,
            scoped=scoped,
            resource_type="custom",
        )

    def update(
        self,
        name: str,
        *,
        description: str | None = None,
        permissions: builtins.list[str] | None = None,
        scoped: bool | None = None,
    ) -> Role:
        """Change a custom role, refusing a core one."""
        config = self.load_config()
        existing = config.roles.get(name)
        if existing is None:
            raise KeyError(f"Role '{name}' not found")
        if existing.resource_type == "core":
            raise ValueError(CORE_RESOURCE_MUTATION_MESSAGE)
        updated = existing.model_copy(
            update={
                k: v
                for k, v in {
                    "description": description,
                    "permissions": permissions,
                    "scoped": scoped,
                }.items()
                if v is not None
            }
        )
        roles = dict(config.roles)
        roles[name] = updated
        self._write_roles(roles)
        return Role(name=name, **updated.model_dump())

    def delete(self, name: str, *, holders: Holders) -> None:
        """Remove a custom role no group holds, refusing a core one.

        Raises:
            KeyError: No role holds the name.
            ValueError: The role is a core one.
            RoleInUseError: A group holds it.
        """
        config = self.load_config()
        if name not in config.roles:
            raise KeyError(f"Role '{name}' not found")
        existing = config.roles[name]
        if existing.resource_type == "core":
            raise ValueError(CORE_RESOURCE_MUTATION_MESSAGE)
        if any(holders(name)):
            raise RoleInUseError(_in_use_message(name))
        roles = dict(config.roles)
        del roles[name]
        self._write_roles(roles)

    def _write_roles(self, roles: dict[str, RoleDefinition]) -> RoleConfig:
        core_names = builtin_core_role_names()
        payload: dict[str, dict[str, object]] = {}
        for role_name, definition in sorted(roles.items()):
            data = definition.model_dump()
            if role_name in core_names:
                data["resource_type"] = "core"
            if not data.get("scoped"):
                data.pop("scoped", None)
            payload[role_name] = data
        yaml_dump({"roles": payload}, self._path)
        return RoleConfig.load(self._path)


class DocuStoreRoleStore:
    """Custom roles in the document store, one document each, beside the core roles -- the same interface as :class:`RoleStore`.

    Core roles are never stored: they are the definitions this engine version ships,
    so every replica answers them alike and an upgrade brings its own. A stored role
    that takes a core role's name is ignored. :meth:`load_config` hands out one
    :class:`~dfe_engine.auth.roles.LiveRoleConfig` that follows the collection.

    A group is given a custom role only through :meth:`bind`, which claims the
    binding before the group is written and then checks the role is still there. A
    delete marks the role first and then looks for groups and claims. Whichever comes
    second sees the other, so a role is never deleted while a group is being given it.

    A mark older than the binding grace belongs to a delete that died before clearing
    it. A bind takes such a mark away, and a delete removes the role only while its own
    mark is still there, so one that was only slow fails rather than racing the bind.
    """

    def __init__(
        self,
        store: DocuStore,
        *,
        collection: str = "roles",
        refresh_seconds: float = REFRESH_SECONDS,
        binding_grace_seconds: float = BINDING_GRACE_SECONDS,
    ) -> None:
        self._c = store.typed(collection, _StoredRole, key="name")
        self._bindings = store.claims(f"{collection}_bindings")
        self._binding_grace = binding_grace_seconds
        self._live = LiveRoleConfig(
            self._definitions,
            lambda: store.revision(collection),
            refresh_seconds=refresh_seconds,
        )

    def load_config(self) -> RoleConfig:
        """The role config every request reads, which follows writes from every replica."""
        return self._live

    def list(self) -> builtins.list[Role]:
        """Every role, in name order."""
        return [
            Role(name=name, **definition.model_dump())
            for name, definition in sorted(self._definitions().items())
        ]

    def get(self, name: str) -> Role | None:
        """The named role, or None."""
        definition = self._definitions().get(name)
        if definition is None:
            return None
        return Role(name=name, **definition.model_dump())

    def create(
        self,
        name: str,
        *,
        description: str,
        permissions: builtins.list[str],
        scoped: bool = False,
    ) -> Role:
        """Add a custom role, refusing a name already taken, including by another replica now."""
        role = _StoredRole(
            name=name,
            description=description,
            permissions=permissions,
            scoped=scoped,
            resource_type="custom",
        )
        if name in builtin_core_role_names() or not self._c.insert(role):
            raise ValueError(f"Role '{name}' already exists")
        self._live.refresh()
        return _public(role)

    def update(
        self,
        name: str,
        *,
        description: str | None = None,
        permissions: builtins.list[str] | None = None,
        scoped: bool | None = None,
    ) -> Role:
        """Change a custom role, refusing a core one."""
        if name in builtin_core_role_names():
            raise ValueError(CORE_RESOURCE_MUTATION_MESSAGE)
        changes = {
            key: value
            for key, value in {
                "description": description,
                "permissions": permissions,
                "scoped": scoped,
            }.items()
            if value is not None
        }
        try:
            role = self._c.mutate(name, lambda stored: stored.model_copy(update=changes))
        except KeyError:
            raise KeyError(f"Role '{name}' not found") from None
        self._live.refresh()
        return _public(role)

    def bind(self, role: str, group: str) -> None:
        """Record that *group* is being given *role*, before the group is written.

        A core role needs no binding: it can never be deleted.

        Raises:
            RoleUnavailableError: *role* is not a role, or its delete is running.
        """
        if role in builtin_core_role_names():
            return
        key = f"{role}/{group}"
        self._bindings.claim(key, group, subject=role)
        # After the claim: a delete that began before it is seen here, one after sees the claim.
        if not self._bindable(role):
            self._bindings.release(key, group)
            raise RoleUnavailableError(f"Role '{role}' does not exist or is being deleted")

    def _bindable(self, role: str) -> bool:
        """Whether *role* exists with no live delete's mark, taking away a mark past the grace."""
        stored = self._c.get(role)
        if stored is None:
            return False
        if not stored.deleting_since:
            return True
        if time.time() - stored.deleting_since < self._binding_grace:
            return False
        try:
            kept = self._c.mutate(role, functools.partial(_unmarked, stored.deleting_since))
        except KeyError:
            return False
        return not kept.deleting_since

    def delete(self, name: str, *, holders: Holders) -> None:
        """Remove a custom role that no group holds or is being given, refusing a core one.

        A binding claim no group holds and older than the grace is a group write that
        never finished, and is released rather than counted. A mark another delete left
        is replaced by this one's.

        Raises:
            KeyError: No role holds the name.
            ValueError: The role is a core one.
            RoleInUseError: A group holds it or is being given it, or a bind took this
                delete's mark away.
        """
        if name in builtin_core_role_names():
            raise ValueError(CORE_RESOURCE_MUTATION_MESSAGE)
        mark = time.time()
        try:
            self._c.mutate(name, lambda stored: stored.model_copy(update={"deleting_since": mark}))
        except KeyError:
            raise KeyError(f"Role '{name}' not found") from None
        try:
            holding = set(holders(name))
            pending = [
                claim
                for claim in self._bindings.about(name)
                if not self._bindings.release_if_orphaned(
                    claim, held=claim.owner in holding, grace_seconds=self._binding_grace
                )
            ]
            if holding or pending:
                raise RoleInUseError(_in_use_message(name))
            if not self._c.delete(name, only_if={"deleting_since": mark}):
                if self._c.exists(name):
                    raise RoleInUseError(_in_use_message(name))
                raise KeyError(f"Role '{name}' not found")
        except BaseException:
            self._unmark(name, mark)
            raise
        self._live.refresh()

    def _unmark(self, name: str, mark: float) -> None:
        """Clear this delete's *mark* from *name*, so groups can be given it again."""
        try:
            self._c.mutate(name, functools.partial(_unmarked, mark))
        except KeyError:
            return

    def _definitions(self) -> dict[str, RoleDefinition]:
        """The core roles this engine ships, then every stored custom role."""
        definitions = dict(RoleConfig.load_builtin().roles)
        for role in self._c.list():
            if role.name not in definitions:
                definitions[role.name] = RoleDefinition(
                    description=role.description,
                    permissions=role.permissions,
                    scoped=role.scoped,
                    resource_type="custom",
                )
        return definitions


def _unmarked(mark: float, stored: _StoredRole) -> _StoredRole:
    """*stored* with *mark* cleared, or unchanged when a later delete marked it since."""
    if stored.deleting_since != mark:
        return stored
    return stored.model_copy(update={"deleting_since": 0.0})


def _public(role: _StoredRole) -> Role:
    """*role* without the store's delete mark."""
    return Role(**role.model_dump(exclude={"deleting_since"}))


def _in_use_message(name: str) -> str:
    return f"Role '{name}' is assigned to one or more groups"
