#  Project:      dfe-engine
#  File:         roles.py
#  Purpose:      RoleConfig with YAML-based role definitions and wildcard permission matching
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Role configuration for DFE RBAC.

Provides YAML-based role definitions with wildcard permission matching.

Wildcard rules:
    "*"                   — matches any action regardless of segment count
    "config:*"            — trailing wildcard: matches "config:read", "config:write",
                            "config:read:sub", etc. (last segment is *, any suffix)
    "argo:*"              — trailing wildcard: matches "argo:applications:sync" (3 segs)
    "service:*:config:*"  — mid wildcard: * matches a single segment; last * is trailing
    "service:*:config:read" — mid wildcard only: exact segment count required (no trailing)

Usage:
    from dfe_engine.auth.roles import RoleConfig

    config = RoleConfig.load_builtin()
    if config.has_permission("infra_admin", "argo:applications:sync"):
        ...
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dfe_engine.yaml_utils import yaml_load, yaml_load_string


def permission_matches(permission: str, action: str) -> bool:
    """Check whether a permission pattern matches a given action.

    Wildcard rules:
      - Bare "*": matches any action (all segment counts).
      - Last segment is "*" AND pattern has fewer or equal segments than action:
          trailing wildcard — the "*" absorbs all remaining action segments.
      - Intermediate "*" segments match exactly one action segment.
      - If last segment is not "*" and segment counts differ → False.

    Args:
        permission: Permission pattern (may contain "*" wildcards).
        action: Action string to test (e.g. "argo:applications:sync").

    Returns:
        True if the permission pattern matches the action.
    """
    # Bare wildcard — matches everything.
    if permission == "*":
        return True

    p_parts = permission.split(":")
    a_parts = action.split(":")

    last_is_wildcard = p_parts[-1] == "*"

    if last_is_wildcard:
        # Trailing wildcard: pattern must not be longer than action.
        # The last "*" absorbs all remaining action segments.
        if len(p_parts) > len(a_parts):
            return False
        # Match all non-wildcard prefix segments
        prefix = p_parts[:-1]
        for p_seg, a_seg in zip(prefix, a_parts, strict=False):
            if p_seg != "*" and p_seg != a_seg:
                return False
        return True
    else:
        # No trailing wildcard: segment counts must match exactly.
        if len(p_parts) != len(a_parts):
            return False
        for p_seg, a_seg in zip(p_parts, a_parts, strict=True):
            if p_seg != "*" and p_seg != a_seg:
                return False
        return True


class RoleDefinition(BaseModel):
    """Definition of a single role with permissions and scope flag."""

    description: str
    permissions: list[str]
    scoped: bool = False


class RoleConfig:
    """Role configuration loaded from YAML.

    Holds role definitions and provides permission-checking methods.
    """

    def __init__(self, roles: dict[str, RoleDefinition]) -> None:
        self.roles = roles

    def has_permission(self, role_name: str, action: str) -> bool:
        """Check whether a single role grants the given action.

        Args:
            role_name: Role to check.
            action: Action string to test.

        Returns:
            True if the role exists and one of its permission patterns matches.
        """
        role = self.roles.get(role_name)
        if role is None:
            return False
        return any(permission_matches(perm, action) for perm in role.permissions)

    def check_roles(self, role_names: list[str], action: str) -> str | None:
        """Return the first role in role_names that grants action, or None.

        Args:
            role_names: Ordered list of role names to check.
            action: Action string to test.

        Returns:
            Name of the first granting role, or None if none grant it.
        """
        for name in role_names:
            if self.has_permission(name, action):
                return name
        return None

    def resolve_permissions(self, role_names: list[str]) -> set[str]:
        """Return the union of all permission patterns across the given roles.

        Args:
            role_names: Role names to collect permissions from.

        Returns:
            Set of permission pattern strings from all matching roles.
        """
        result: set[str] = set()
        for name in role_names:
            role = self.roles.get(name)
            if role is not None:
                result.update(role.permissions)
        return result

    @classmethod
    def load(cls, path: Path) -> RoleConfig:
        """Load role configuration from a YAML file.

        Args:
            path: Path to the YAML file. Must exist.

        Returns:
            RoleConfig populated from the file.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the YAML structure is invalid.
        """
        data: dict[str, Any] = yaml_load(path)
        return cls._from_data(data)

    @classmethod
    def load_builtin(cls) -> RoleConfig:
        """Load the built-in default role definitions.

        Reads from the package resource at dfe_engine/auth/resources/roles.yaml.

        Returns:
            RoleConfig with the default DFE role set.
        """
        pkg = importlib.resources.files("dfe_engine.auth.resources")
        resource = pkg.joinpath("roles.yaml")
        content = resource.read_text(encoding="utf-8")
        data: dict[str, Any] = yaml_load_string(content)
        return cls._from_data(data)

    @classmethod
    def _from_data(cls, data: dict[str, Any]) -> RoleConfig:
        """Parse a roles dict and return a RoleConfig.

        Args:
            data: Parsed YAML dict with a "roles" key.

        Returns:
            RoleConfig instance.

        Raises:
            ValueError: If the data is missing the "roles" key.
        """
        if "roles" not in data:
            raise ValueError("YAML must contain a 'roles' key")
        roles: dict[str, RoleDefinition] = {}
        for name, raw in data["roles"].items():
            roles[name] = RoleDefinition.model_validate(raw)
        return cls(roles=roles)
