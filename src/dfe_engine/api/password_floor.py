#  Project:      dfe-engine
#  File:         api/password_floor.py
#  Purpose:      The length floor on a password set through the API, and its refusal count
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The length floor on every password the API sets, and the count of its refusals.

The request models carry :data:`~dfe_engine.auth.bootstrap.MIN_ADMIN_PASSWORD_LENGTH`
as ``min_length``, so a short password is refused with a 422 before any route runs,
and the validation handler reports each refusal here. SCIM parses its own body, so
it checks the floor with :func:`below_floor` and reports the refusal the same way.
Neither path echoes the password: the 422 names the field and the floor, never the
value.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from fastapi import Request

from dfe_engine.api.metrics import ApiMetrics
from dfe_engine.auth.bootstrap import MIN_ADMIN_PASSWORD_LENGTH

# Request-body fields that carry a password the API stores.
PASSWORD_FIELDS = frozenset({"password", "new_password"})

FLOOR_MESSAGE = f"password must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters"


def below_floor(password: str) -> bool:
    """Whether *password* is shorter than the floor."""
    return len(password) < MIN_ADMIN_PASSWORD_LENGTH


def is_floor_refusal(errors: Iterable[Mapping[str, Any]]) -> bool:
    """Whether a request's validation errors include a password under the floor.

    Args:
        errors: The request's validation errors, as ``RequestValidationError.errors()``.

    Returns:
        True when one of them is a password field refused by the floor's ``min_length``.
    """
    for error in errors:
        loc = error.get("loc") or ()
        ctx = error.get("ctx") or {}
        if (
            error.get("type") == "string_too_short"
            and loc
            and loc[-1] in PASSWORD_FIELDS
            and ctx.get("min_length") == MIN_ADMIN_PASSWORD_LENGTH
        ):
            return True
    return False


def count_floor_refusal(request: Request) -> None:
    """Count a password write refused under the floor, labelled by its route template."""
    metrics = getattr(request.app.state, "api_metrics", None) or ApiMetrics()
    route = str(getattr(request.scope.get("route"), "path", "")) or "other"
    metrics.password_floor_refused(route)


__all__ = [
    "FLOOR_MESSAGE",
    "PASSWORD_FIELDS",
    "below_floor",
    "count_floor_refusal",
    "is_floor_refusal",
]
