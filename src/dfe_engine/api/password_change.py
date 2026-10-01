#  Project:      dfe-engine
#  File:         api/password_change.py
#  Purpose:      Refuse an account the API until it replaces an issued password
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The gate in front of an account whose password was issued rather than chosen.

Every deployment mints the bootstrap admin's password and hands it over through
its secret store, so anyone who can read that store holds it. The account is
therefore created with ``password_change_required`` set (:mod:`dfe_engine.auth.bootstrap`),
and until its owner replaces the password the API answers every authenticated
route with 403 ``password_change_required``, except the ones the change itself
needs.

Login still succeeds and says a change is required, so a console can sign the
owner in and put the change screen in front of everything else. The owner may
also log out. The setup status is public and never reaches this gate.
"""

from typing import Any

from fastapi import HTTPException, Request, status

from dfe_engine.api.errors import ErrorCode
from dfe_engine.api.metrics import ApiMetrics

# Token claim marking a session on an issued password; dfe-hyperdx refuses such a token.
PASSWORD_CHANGE_CLAIM = "password_change_required"  # noqa: S105, RUF100 - a JWT claim name

# The owner's own password change, which clears the flag.
CHANGE_PASSWORD_ROUTE = ("POST", "/api/v1/auth/accounts/reset-password")

# Routes a session may call while its password must change: the change itself, the
# token refresh that keeps the session alive through it, logging out, and the reads
# that tell a console who is signed in and whether the account can set a local
# password. None takes a path parameter, so each matches the request path exactly.
ALLOWED_BEFORE_CHANGE: frozenset[tuple[str, str]] = frozenset(
    {
        CHANGE_PASSWORD_ROUTE,
        ("POST", "/api/v1/auth/refresh"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/auth/accounts/me"),
    }
)


def _request_path(request: Request) -> str:
    """The path the app routed on, without any root path a proxy mounted it under."""
    path = str(request.scope.get("path", ""))
    root = str(request.scope.get("root_path", ""))
    return path.removeprefix(root) if root else path


def _area(request: Request) -> str:
    """The matched route's first literal segment, so a path parameter never reaches a label."""
    template = str(getattr(request.scope.get("route"), "path", ""))
    segment = template.lstrip("/").split("/", 1)[0]
    return "other" if not segment or segment.startswith("{") else segment


def refuse_until_password_changed(request: Request, account: Any) -> None:
    """Refuse the request when *account* must change its password first.

    Args:
        request: The request being authenticated.
        account: The session's store account, or None when it has none.

    Raises:
        HTTPException: 403 ``password_change_required`` on any route outside
            :data:`ALLOWED_BEFORE_CHANGE`.
    """
    if account is None or not getattr(account, "password_change_required", False):
        return
    if (request.method, _request_path(request)) in ALLOWED_BEFORE_CHANGE:
        return
    metrics = getattr(request.app.state, "api_metrics", None) or ApiMetrics()
    metrics.password_change_refused(request.method, _area(request))
    method, change_path = CHANGE_PASSWORD_ROUTE
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": ErrorCode.PASSWORD_CHANGE_REQUIRED,
            "message": (
                f"Account '{account.username}' is on an issued password and must change "
                f"it before using the API: {method} {change_path}"
            ),
        },
    )


__all__ = [
    "ALLOWED_BEFORE_CHANGE",
    "CHANGE_PASSWORD_ROUTE",
    "PASSWORD_CHANGE_CLAIM",
    "refuse_until_password_changed",
]
