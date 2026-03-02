"""DFE authorization module.

Bespoke role→permission RBAC. Zero external dependencies.

Usage:
    from dfe_engine.auth import authorize, AuthContext

    # Dev/test (auth disabled) — always allowed:
    result = authorize(None, "config:read")

    # Production (auth enabled):
    auth = AuthContext(org_id="acme", user_id="alice", roles=["operator"])
    result = authorize(auth, "config:write")
    if not result.allowed:
        raise AuthorizationError(result.reason)

The authorize(auth, action, resource) signature maps 1:1 to the
Cedar/OPAL model. If DFE outgrows bespoke RBAC, swap the engine
implementation — callers don't change.
"""

from dfe_engine.auth.engine import (
    ALL_ACTIONS,
    ARGO_ACTION_PREFIX,
    DEFAULT_ROLE_PERMISSIONS,
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
)

__all__ = [
    "ALL_ACTIONS",
    "ARGO_ACTION_PREFIX",
    "AuthContext",
    "AuthenticationError",
    "AuthorizationError",
    "AuthzRequest",
    "AuthzResult",
    "DEFAULT_ROLE_PERMISSIONS",
    "ENGINE_ACTIONS",
    "LocalAuthProvider",
    "authorize",
]
