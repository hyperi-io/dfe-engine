"""Authentication and authorization models.

AuthContext is the canonical identity model for DFE -- extracted from JWT
claims by dfe-control-plane and passed into engine for Cedar evaluation.
"""

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dfe_engine.auth.roles import RoleConfig, permission_covers


class AuthenticationError(Exception):
    """Authentication failed (bad credentials, disabled account, etc.)."""


class AuthorizationError(Exception):
    """User not authorized for the requested action."""


ScopeType = Literal["system", "org", "group", "user"]


class Scope(BaseModel):
    """Where a grant applies (or where an action is requested).

    Scope is orthogonal to the action string: roles keep their global
    permission patterns, and a grant carries the scope it was bound at.
    Coverage is grant-only union - system covers everything, an org
    covers itself and its own groups, group/user cover only themselves.
    An unscoped require_action() check is a SYSTEM-scope check: "no
    scope" never implicitly means "all scopes".
    """

    model_config = ConfigDict(frozen=True)

    type: ScopeType = "system"
    id: str = Field(default="", description="Org name, group name, or username ('' for system)")
    org: str = Field(
        default="",
        description="Owning org for group scopes ('' = system-wide group)",
    )

    def covers(self, requested: Scope) -> bool:
        """True if a grant at this scope satisfies a check at ``requested``."""
        if self.type == "system":
            return True
        if self.type == "org":
            if requested.type == "org":
                return requested.id == self.id
            if requested.type == "group":
                # An org covers only groups that belong to it - never
                # system-wide groups (org admins don't manage those).
                return requested.org == self.id and requested.org != ""
            return False
        if self.type == "group":
            return (
                requested.type == "group" and requested.id == self.id and requested.org == self.org
            )
        return requested.type == "user" and requested.id == self.id

    def __str__(self) -> str:
        if self.type == "system":
            return "system"
        if self.type == "group" and self.org:
            return f"group:{self.org}/{self.id}"
        return f"{self.type}:{self.id}"


class ScopedGrant(BaseModel):
    """A role bound at a scope - the unit authorize() evaluates."""

    model_config = ConfigDict(frozen=True)

    role: str
    scope: Scope = Field(default_factory=Scope)
    org_ids: tuple[str, ...] = Field(
        default=(),
        description="The org markers of the group the grant came from: its owning org "
        "and its org_ids, which a system-bound tenant role is held to",
    )


# The tenant role: holding it never unfences a group or a caller from its org.
ORG_VIEWER_ROLE = "org_viewer"


def is_platform_role(role: str, role_config: RoleConfig) -> bool:
    """Whether a system-scope grant of ``role`` may read across orgs.

    Only a role the definitions declare and do not mark ``scoped`` may. ``org_viewer``
    never may, whatever a deployment's definitions say, and an undefined role grants
    nothing, so it unfences nothing either.

    Args:
        role: The role name.
        role_config: The role definitions ``authorize()`` reads.

    Returns:
        True for a declared, unscoped role other than ``org_viewer``.
    """
    if role == ORG_VIEWER_ROLE:
        return False
    definition = role_config.roles.get(role)
    return definition is not None and not definition.scoped


def platform_grants(
    grants: Iterable[ScopedGrant], *, role_config: RoleConfig | None = None
) -> list[ScopedGrant]:
    """Return the grants that may read across orgs: SYSTEM scope, of a platform role.

    A role bound at an org's scope covers that org alone, and a ``scoped`` role is
    held to its holder's orgs wherever it is bound (:func:`is_platform_role`). The
    ClickHouse group bindings and their otel reader, the sampler, the HyperDX
    connection read, the fork's role claim and JIT team assignment all decide
    "every org" through this one filter.

    Args:
        grants: The grants to filter.
        role_config: The role definitions ``authorize()`` reads; None reads the
            shipped ones, as ``authorize()`` does.

    Returns:
        The grants that may read across orgs, in their given order.
    """
    config = role_config or RoleConfig.load_builtin()
    return [g for g in grants if g.scope.type == "system" and is_platform_role(g.role, config)]


class AuthContext(BaseModel):
    """Authentication context extracted from JWT.

    In production, populated from Envoy OIDC headers by dfe-control-plane.
    In dev/test, omitted entirely (engine operates as root).

    These values are injected as reserved parameters (_org_id, etc.)
    and cannot be overridden by clients.
    """

    org_id: str = Field(
        default="",
        description="The org the session's account is bound to, empty when it is bound to none",
    )
    user_id: str = Field(..., description="User ID")
    email: str | None = Field(
        default=None,
        description="User email (from OIDC/JWT claim) for git author attribution; "
        "None for local accounts, API keys, and dev mode",
    )
    roles: list[str] = Field(default_factory=list)
    grants: list[ScopedGrant] = Field(
        default_factory=list,
        description="Roles with the scope each was bound at (resolved from group "
        "membership). Empty falls back to treating `roles` as system-scope grants.",
    )
    org_ids: list[str] = Field(
        default_factory=list, description="Org IDs for customer-scoped roles"
    )
    connection_id: str = Field(default="", description="Resolved CH connection name")
    groups: list[str] = Field(default_factory=list, description="OIDC groups (Entra ID, Okta)")
    request_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None


def platform_caller(
    user: AuthContext, *, role_config: RoleConfig | None = None
) -> AuthContext | None:
    """Return ``user`` holding only the grants ``platform_grants`` keeps, or None if none.

    Authorising an action against the result asks whether the caller may take it
    across every org. Bare roles count as system-scope grants, as ``authorize()``
    reads a context without grants.

    Args:
        user: The caller's auth context.
        role_config: The role definitions ``authorize()`` reads; None reads the
            shipped ones.

    Returns:
        A copy of ``user`` with those grants and their roles, or None when it holds none.
    """
    platform = platform_grants(held_grants(user), role_config=role_config)
    if not platform:
        return None
    return user.model_copy(update={"roles": [grant.role for grant in platform], "grants": platform})


def held_grants(user: AuthContext) -> list[ScopedGrant]:
    """The grants ``authorize()`` reads for ``user``: its scoped grants, else its roles system-wide.

    A bare role carries the context's ``org_ids``, the orgs a context with no group
    behind its roles has always been held to.

    Args:
        user: The caller's auth context.

    Returns:
        The caller's grants.
    """
    return user.grants or [
        ScopedGrant(role=name, org_ids=tuple(user.org_ids)) for name in user.roles
    ]


def ungrantable_permissions(
    user: AuthContext,
    permissions: Iterable[str],
    scope: Scope,
    *,
    across_orgs: bool,
    role_config: RoleConfig,
) -> list[str]:
    """The ``permissions`` the caller holds through no grant covering ``scope``.

    A write handing out a permission, or taking it away from whoever holds it, may
    do so only for one the caller holds where the write lands. A permission bound to
    read across orgs counts only when the caller reads across orgs with it too.

    Args:
        user: The caller's auth context.
        permissions: The permission patterns the write hands out or takes away.
        scope: Where the write binds them.
        across_orgs: Whether the write binds them across orgs, so only
            :func:`platform_grants` count.
        role_config: The role definitions ``authorize()`` reads.

    Returns:
        The patterns the caller does not hold, each once, in their given order.
    """
    grants = held_grants(user)
    if across_orgs:
        grants = platform_grants(grants, role_config=role_config)
    covering = [grant.role for grant in grants if grant.scope.covers(scope)]
    held = role_config.resolve_permissions(covering)
    missing = [p for p in permissions if not any(permission_covers(h, p) for h in held)]
    return list(dict.fromkeys(missing))


def ungrantable_roles(
    user: AuthContext, roles: Iterable[str], scope: Scope, *, role_config: RoleConfig
) -> list[str]:
    """The ``roles`` the caller may not bind at ``scope``, sorted.

    A role is grantable when the caller holds every permission it grants at a scope
    covering ``scope`` (:func:`ungrantable_permissions`). A system-scope binding of a
    platform role reads every org, so it needs platform grants. A role the
    definitions do not declare can be declared as anything later, so only a caller
    holding ``*`` there may bind it.

    Args:
        user: The caller's auth context.
        roles: The role names the write binds or unbinds.
        scope: The scope the roles bind at.
        role_config: The role definitions ``authorize()`` reads.

    Returns:
        The roles the caller may not hand out or take away.
    """
    refused = []
    for role in sorted(set(roles)):
        definition = role_config.roles.get(role)
        permissions = ["*"] if definition is None else definition.permissions
        across_orgs = scope.type == "system" and (
            definition is None or is_platform_role(role, role_config)
        )
        missing = ungrantable_permissions(
            user, permissions, scope, across_orgs=across_orgs, role_config=role_config
        )
        if missing:
            refused.append(role)
    return refused


class AuthzRequest(BaseModel):
    """A single authorization request for batch evaluation."""

    principal: str = Field(..., description='Cedar principal (e.g. User::"alice")')
    action: str = Field(..., description='Cedar action (e.g. Action::"config:read")')
    resource: str = Field(
        ..., description='Cedar resource (e.g. ServiceConfig::"receiver-production")'
    )
    context: dict[str, Any] = Field(default_factory=dict)


class AuthzResult(BaseModel):
    """Result of an authorization evaluation."""

    allowed: bool
    reason: str = ""
