#  Project:      dfe-engine
#  File:         auth/hyperdx_role.py
#  Purpose:      The role claim the engine puts on the tokens dfe-hyperdx verifies
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The ``role`` claim dfe-hyperdx reads off an engine token.

The fork gates deleting and restoring the shipped dashboards on this claim: a
token carrying ``admin`` or ``owner`` may change what the whole team sees, any
other value is refused with 403, and a token with no claim at all keeps the old
team-membership behaviour. So the value is always written - an account with no
team-admin role carries :data:`MEMBER`, which is a refusal rather than a
fallback (dfe-hyperdx ``packages/api/src/dfe/middleware/role-claim.ts``).

The engine resolves one role set per account, as the union across its groups, so
the claim is the account's standing across the deployment; the fork applies it to
whichever team the token's groups select. Nothing in the engine's own
authorisation reads the claim back - ``deps.py`` resolves roles from the group
files on every request and ignores what a token asserts.
"""

from __future__ import annotations

from collections.abc import Iterable

CLAIM = "role"
"""The claim name the fork reads (its ``jwt-verify.ts`` takes ``payload.role``)."""

TEAM_ADMIN = "admin"
"""What an account holding a team-admin engine role carries."""

MEMBER = "member"
"""What everyone else carries - present, and outside what the fork accepts."""

FORK_ACCEPTS = frozenset({"admin", "owner"})
"""The values the fork allows; it refuses every other one, ``member`` included."""

TEAM_ADMIN_ROLES = frozenset({"admin", "infra_admin"})
"""Engine roles that may change what a whole HyperDX team sees.

``admin`` holds every permission there is, and ``infra_admin`` owns the
deployment-provisioned artefacts the shipped dashboards are one of. Every other
role - the data and viewer families included - is a team member with no say over
what the team sees.
"""

SERVICE_ROLE = TEAM_ADMIN
"""The engine's own machine identity provisions the shipped dashboards."""


def role_claim(roles: Iterable[str]) -> str:
    """The fork's role for an account holding these engine roles."""
    return TEAM_ADMIN if TEAM_ADMIN_ROLES.intersection(roles) else MEMBER
