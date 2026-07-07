#  Project:      dfe-engine
#  File:         roles.py
#  Purpose:      RoleConfig with YAML-based role definitions and wildcard permission matching
#  Language:     Python
#
#  License:      BUSL-1.1
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
    if config.has_permission("infra", "argo:applications:sync"):
        ...
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from dfe_engine.yaml_utils import yaml_load, yaml_load_string

RoleResourceType = Literal["core", "custom"]

# HyperDX capability a role may declare in its ``hyperdx:`` block. Widest wins
# in cumulative resolution (see _HYPERDX_ACCESS_RANK): full covers everything,
# none is no access (the default when the block is absent).
HyperdxAccessLevel = Literal["full", "otel", "org-scoped", "none"]

# Lower rank = wider access. effective_hyperdx() picks the minimum rank across a
# user's roles, so a single "full" role beats any number of narrower ones.
_HYPERDX_ACCESS_RANK: dict[str, int] = {
    "full": 0,
    "otel": 1,
    "org-scoped": 2,
    "none": 3,
}

_BUILTIN_CORE_ROLE_NAMES: frozenset[str] | None = None


def builtin_core_role_names() -> frozenset[str]:
    """Role names shipped in ``auth/resources/roles.yaml``."""
    global _BUILTIN_CORE_ROLE_NAMES
    if _BUILTIN_CORE_ROLE_NAMES is None:
        pkg = importlib.resources.files("dfe_engine.auth.resources")
        resource = pkg.joinpath("roles.yaml")
        data: dict[str, Any] = yaml_load_string(resource.read_text(encoding="utf-8"))
        roles = data.get("roles") or {}
        _BUILTIN_CORE_ROLE_NAMES = frozenset(roles.keys())
    return _BUILTIN_CORE_ROLE_NAMES


def _normalize_role_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept the hyphenated key spelling as an alias for ``resource_type``.

    The original guard read ``"resource_type" in data and "resource_type" not
    in data`` - a contradiction that was always False, so the intended
    normalisation never ran. RoleDefinition ignores unknown keys, so a role
    authored with the natural hyphenated ``resource-type`` would silently lose
    its declared type. Fold the hyphenated alias onto the canonical underscore
    key (underscore wins if both are present).
    """
    data = dict(raw)
    if "resource-type" in data and "resource_type" not in data:
        data["resource_type"] = data.pop("resource-type")
    return data


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


class HyperdxAccess(BaseModel):
    """A role's declared HyperDX capability (config-driven, not code).

    access:
        full       - full HyperDX: all connections / dashboards
        otel       - the OTel self-monitoring stream only (infra posture)
        org-scoped - only the user's own org data
        none       - no HyperDX access; the default when the block is absent
    tenant_scoped:
        True when the role's HyperDX view must be pinned to the user's org.
    """

    model_config = ConfigDict(extra="ignore")

    access: HyperdxAccessLevel = "none"
    tenant_scoped: bool = False


class RoleDefinition(BaseModel):
    """Definition of a single role with permissions and scope flag."""

    model_config = ConfigDict(extra="ignore")

    description: str
    permissions: list[str]
    scoped: bool = False
    resource_type: RoleResourceType = "custom"
    # Optional config-driven HyperDX capability. Absent (None) == no access, so
    # a pre-hyperdx role stays backward-compatible. See HyperdxAccess.
    hyperdx: HyperdxAccess | None = None


class RoleConfig:
    """Role configuration loaded from YAML.

    Holds role definitions and provides permission-checking methods.
    """

    def __init__(self, roles: dict[str, RoleDefinition]) -> None:
        self.roles = roles

    def _lookup(self, role_name: str) -> RoleDefinition | None:
        """Resolve a role name to its definition (exact match, no aliasing)."""
        return self.roles.get(role_name)

    def has_permission(self, role_name: str, action: str) -> bool:
        """Check whether a single role grants the given action.

        Args:
            role_name: Role to check.
            action: Action string to test.

        Returns:
            True if the role exists and one of its permission patterns matches.
        """
        role = self._lookup(role_name)
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
            role = self._lookup(name)
            if role is not None:
                result.update(role.permissions)
        return result

    def hyperdx_for(self, role_name: str) -> HyperdxAccess | None:
        """Return a role's declared HyperDX capability, or None if undeclared.

        A role that ships no ``hyperdx:`` block returns None, which the
        cumulative resolver treats as access="none".
        """
        role = self._lookup(role_name)
        if role is None:
            return None
        return role.hyperdx

    def effective_hyperdx(self, role_names: list[str]) -> HyperdxAccess:
        """Cumulative HyperDX capability across a set of roles.

        Roles are cumulative, so the WIDEST access any role grants wins
        (full > otel > org-scoped > none). The result is tenant_scoped only
        when EVERY contributing role is org-scoped - a single non-org-scoped
        HyperDX role lifts the org restriction. Roles with no ``hyperdx:`` block
        (or access="none") contribute nothing; with no contributors the result
        is access="none", tenant_scoped=False.
        """
        contributing = [
            access
            for name in role_names
            if (access := self.hyperdx_for(name)) is not None and access.access != "none"
        ]
        if not contributing:
            return HyperdxAccess(access="none", tenant_scoped=False)
        widest = min(contributing, key=lambda h: _HYPERDX_ACCESS_RANK[h.access])
        tenant_scoped = all(h.access == "org-scoped" for h in contributing)
        return HyperdxAccess(access=widest.access, tenant_scoped=tenant_scoped)

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
        core_names = builtin_core_role_names()
        for name, raw in data["roles"].items():
            if not isinstance(raw, dict):
                raise ValueError(f"Role '{name}' must be a mapping")
            normalized = _normalize_role_raw(raw)
            if name in core_names:
                normalized["resource_type"] = "core"
            else:
                normalized.setdefault("resource_type", "custom")
            roles[name] = RoleDefinition.model_validate(normalized)
        return cls(roles=roles)
