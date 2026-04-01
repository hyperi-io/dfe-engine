#  Project:      dfe-engine
#  File:         groups.py
#  Purpose:      YAML-backed group store for managing groups with role mappings
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from dfe_engine.yaml_utils import yaml_dump, yaml_load


class Group(BaseModel):
    """A named group with roles and member usernames."""

    name: str
    description: str = ""
    roles: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    source_provider: str = ""
    """Name of the OIDC provider that owns this group (empty for manually managed groups)."""
    source_id: str = ""
    """Provider-specific group identifier (e.g. Google group key, Entra object ID)."""


class GroupStore:
    """YAML-backed store for managing groups with role mappings.

    Each group is persisted as ``{name}.yaml`` under the groups directory.
    The group name is the filename stem — it is NOT stored inside the YAML file.

    Example layout::

        groups/
            admins.yaml     # { description: ..., roles: [...], members: [...] }
            operators.yaml
    """

    def __init__(self, groups_dir: Path) -> None:
        self._dir = Path(groups_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self._dir / f"{name}.yaml"

    def _read(self, name: str) -> Group | None:
        path = self._path(name)
        if not path.exists():
            return None
        data = yaml_load(path)
        if data is None:
            data = {}
        # Name is derived from filename, not stored in the file
        data["name"] = path.stem
        return Group.model_validate(data)

    def _write(self, group: Group) -> None:
        # Exclude the name field — it lives in the filename, not the YAML
        data = group.model_dump(exclude={"name"})
        yaml_dump(data, self._path(group.name))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, name: str, roles: list[str], description: str = "") -> Group:
        """Create a new group and persist it to YAML.

        Raises:
            ValueError: If a group with this name already exists.
        """
        if self._path(name).exists():
            raise ValueError(f"Group '{name}' already exists")
        group = Group(name=name, roles=roles, description=description)
        self._write(group)
        return group

    def get(self, name: str) -> Group | None:
        """Return the named group, or None if it does not exist."""
        return self._read(name)

    def list(self) -> list[Group]:
        """Return all groups sorted by name."""
        groups = []
        for path in sorted(self._dir.glob("*.yaml")):
            group = self._read(path.stem)
            if group is not None:
                groups.append(group)
        return groups

    def update(self, name: str, **fields: object) -> Group:
        """Update one or more fields on an existing group and persist.

        Accepted keyword arguments: ``roles``, ``description``, ``members``.

        Raises:
            KeyError: If the group does not exist.
        """
        group = self._read(name)
        if group is None:
            raise KeyError(f"Group '{name}' not found")
        updated = group.model_copy(update=fields)
        self._write(updated)
        return updated

    def delete(self, name: str) -> None:
        """Delete the named group.

        Raises:
            KeyError: If the group does not exist.
        """
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"Group '{name}' not found")
        path.unlink()

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

    def remove_member(self, group_name: str, username: str) -> None:
        """Remove a username from the group's member list.

        Removing a user who is not in the group is a no-op.

        Raises:
            KeyError: If the group does not exist.
        """
        group = self._read(group_name)
        if group is None:
            raise KeyError(f"Group '{group_name}' not found")
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
