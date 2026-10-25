#  Project:      dfe-engine
#  File:         auth/hyperdx_role.py
#  Purpose:      The role claim the engine puts on the tokens dfe-hyperdx verifies
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The role dfe-hyperdx gates team-wide changes and every write on.

The fork reads one of three values. ``admin`` (or ``owner``) may change what the
whole team sees, such as deleting and restoring the shipped dashboards. ``member``
may save searches and dashboards but not change those. ``viewer`` is refused every
write, server side. A session with no value at all keeps the old team-membership
behaviour, so the value is always written (dfe-hyperdx
``packages/api/src/dfe/middleware/role-claim.ts``).

The fork reads it from ``GET /api/v1/auth/me`` (``hyperdx_role``), resolved from
the account's groups at that request, and falls back to the token's ``role``
claim only when the engine's answer carries none.

The engine resolves one grant set per account, as the union across its groups, so
the value is the account's standing across the deployment, and the fork applies it
to whichever team the session lands on. That is why only a system-scope grant makes
an admin: an admin bound at one org's scope must not change what another org's
team sees. Authoring is ``query:write`` (:data:`AUTHOR_ACTION`), held at any scope.
Nothing in the engine's own authorisation reads the claim back - ``deps.py``
resolves grants from the group files on every request and ignores what a token
asserts.
"""

from collections.abc import Iterable

from dfe_engine.auth.models import ScopedGrant, platform_grants
from dfe_engine.auth.roles import RoleConfig

CLAIM = "role"
"""The token claim the fork falls back to when ``/auth/me`` carries no ``hyperdx_role``."""

TEAM_ADMIN = "admin"
"""What an account holding a team-admin engine role carries."""

MEMBER = "member"
"""What an account that may author searches and dashboards carries."""

VIEWER = "viewer"
"""What every other account carries: the fork refuses it every write."""

AUTHOR_ACTION = "query:write"
"""The engine permission that saves searches and dashboards in HyperDX."""

FORK_ACCEPTS = frozenset({"admin", "owner"})
"""The values the fork allows a team-wide change; it refuses every other one."""

TEAM_ADMIN_ROLES = frozenset({"admin", "infra_admin"})
"""Engine roles that may change what a whole HyperDX team sees.

``admin`` holds every permission there is, and ``infra_admin`` owns the
deployment-provisioned artefacts the shipped dashboards are one of. Every other
role - the data and viewer families included - is a team member with no say over
what the team sees.
"""

SERVICE_ROLE = TEAM_ADMIN
"""The engine's own machine identity provisions the shipped dashboards."""


def role_claim(grants: Iterable[ScopedGrant], role_config: RoleConfig | None = None) -> str:
    """The fork's role for an account holding these grants.

    Args:
        grants: The account's grants.
        role_config: The role definitions ``authorize()`` reads; None reads the
            shipped ones.

    Returns:
        :data:`TEAM_ADMIN` for a team-admin role at system scope, else :data:`MEMBER`
        when a grant's role allows :data:`AUTHOR_ACTION`, else :data:`VIEWER`.
    """
    held = list(grants)
    config = role_config or RoleConfig.load_builtin()
    platform_roles = {grant.role for grant in platform_grants(held, role_config=config)}
    if TEAM_ADMIN_ROLES & platform_roles:
        return TEAM_ADMIN
    if any(config.has_permission(grant.role, AUTHOR_ACTION) for grant in held):
        return MEMBER
    return VIEWER
