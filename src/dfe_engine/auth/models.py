"""Authentication and authorization models.

AuthContext is the canonical identity model for DFE -- extracted from JWT
claims by dfe-control-plane and passed into engine for Cedar evaluation.
"""

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dfe_engine.auth.roles import RoleConfig


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

    org_id: str = Field(default="default", description="Primary tenant org ID")
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
    grants = user.grants or [ScopedGrant(role=name) for name in user.roles]
    platform = platform_grants(grants, role_config=role_config)
    if not platform:
        return None
    return user.model_copy(update={"roles": [grant.role for grant in platform], "grants": platform})


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
