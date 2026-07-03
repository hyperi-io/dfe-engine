#  Project:      dfe-engine
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""DFE authorization module.

Bespoke role->permission RBAC backed by YAML role definitions.

Usage:
    from dfe_engine.auth import authorize, AuthContext

    # Dev/test (auth disabled) -- always allowed:
    result = authorize(None, "config:read")

    # Production (auth enabled):
    auth = AuthContext(org_id="acme", user_id="alice", roles=["data_analyst"])
    result = authorize(auth, "config:write")
    if not result.allowed:
        raise AuthorizationError(result.reason)

The authorize(auth, action, resource) signature maps 1:1 to the
Cedar/OPAL model. If DFE outgrows bespoke RBAC, swap the engine
implementation -- callers don't change.
"""

from dfe_engine.auth.engine import (
    ARGO_ACTION_PREFIX,
    ENGINE_ACTIONS,
    authorize,
)
from dfe_engine.auth.local_provider import LocalAuthProvider
from dfe_engine.auth.models import (
    AuthContext,
    AuthenticationError,
    AuthorizationError,
    AuthzRequest,
    AuthzResult,
    Scope,
    ScopedGrant,
)
from dfe_engine.auth.roles import RoleConfig, RoleDefinition

__all__ = [
    "ARGO_ACTION_PREFIX",
    "ENGINE_ACTIONS",
    "AuthContext",
    "AuthenticationError",
    "AuthorizationError",
    "AuthzRequest",
    "AuthzResult",
    "LocalAuthProvider",
    "RoleConfig",
    "RoleDefinition",
    "Scope",
    "ScopedGrant",
    "authorize",
]
