#  Project:      dfe-engine
#  File:         api/errors.py
#  Purpose:      Engine problem codes, the domain exceptions they answer, and backend-text hiding
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Engine errors as RFC 9457 problem+json, through scalo's ``Problem``.

A route raises :class:`scalo.api.ProblemError` with a stable snake_case ``code``.
The domain exceptions below are answered the same way wherever they are raised.
"""

from collections.abc import Sequence
from typing import Any, NoReturn

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from scalo.api import ProblemError, problem_response
from scalo.logger import logger
from starlette.routing import BaseRoute

from dfe_engine.api.middleware.access_log import route_template
from dfe_engine.http_errors import failure_reference, referenced
from dfe_engine.log_limit import LineLimit

TOPICS_NOT_CREATED = "topics_not_created"


# -- Error codes ----------------------------------------------


class ErrorCode:
    """Machine-readable problem codes the console branches on."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    INVALID_NAME = "invalid_name"
    CONFLICT = "conflict"
    PROTECTED_ACCOUNT = "protected_account"
    PASSWORD_CHANGE_REQUIRED = "password_change_required"  # noqa: S105, RUF100 - an error code, not a password
    INVALID_SQL = "invalid_sql"
    RULE_MATCHES_EVERYTHING = "rule_matches_everything"
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"
    UNRESOLVED_REFERENCE = "unresolved_reference"
    TOO_MANY_TASKS = "too_many_tasks"


SERVICE_UNAVAILABLE_MESSAGE = "Backing service is unreachable, retry shortly"
GITOPS_UNAVAILABLE_MESSAGE = "The deploy repo is unreachable, retry after the Retry-After interval"


def hide_backend_text(
    message: str,
    exc: BaseException,
    *,
    event: str,
    limit: LineLimit | None = None,
    **fields: Any,
) -> str:
    """Log ``exc``'s text under ``event`` and return ``message`` for the caller in its place.

    A backend's own error text can carry statement fragments, user names and password
    hashes, so it reaches the engine log and never a response.

    Args:
        message: What the caller is told instead.
        exc: The backend failure whose text is logged.
        event: The log line's message.
        limit: Writes the line at most once a period, for an event any caller can repeat.
        **fields: Further structured fields for the log line.

    Returns:
        ``message``, unchanged.
    """
    if limit is None:
        logger.warning(event, error=str(exc), **fields)
    else:
        limit.warning(event, error=str(exc), **fields)
    return message


def backend_failure(
    status_code: int,
    code: str,
    message: str,
    exc: BaseException,
    *,
    event: str,
    headers: dict[str, str] | None = None,
    reference: str = "",
    limit: LineLimit | None = None,
    **fields: Any,
) -> ProblemError:
    """The ProblemError a route raises for a failure whose text is logged instead of sent.

    The answer and the log line carry the same reference, so whoever reads the
    answer can find the reason.

    Args:
        status_code: The HTTP status to answer.
        code: The machine-readable error code.
        message: What the caller is told, before the reference.
        exc: The failure whose text is logged.
        event: The log line's message.
        headers: Response headers, such as a 401's ``WWW-Authenticate``.
        reference: The reference to answer when another line already carries it. Empty
            makes a fresh one.
        limit: Writes the line at most once a period. A caller passing one logs the
            reference on a line of its own, since a held-back line carries none.
        **fields: Further structured fields for the log line.

    Returns:
        The exception to raise, chained from ``exc`` by the caller.
    """
    reference = reference or failure_reference()
    told = hide_backend_text(message, exc, event=event, limit=limit, reference=reference, **fields)
    return ProblemError(status_code, referenced(told, reference), code=code, headers=headers)


def engine_message(exc: BaseException, fallback: str, *, event: str, **fields: Any) -> str:
    """``exc``'s own message when the engine wrote it, else ``fallback`` with its text logged.

    The error types this serves are raised ``from`` a backend exception whenever they
    carry that backend's text, and with no cause when the engine composed the message.

    Args:
        exc: The error a route is about to report.
        fallback: What the caller is told when ``exc`` carries a backend's text.
        event: The log line's message.
        **fields: Further structured fields for the log line.

    Returns:
        The message that is safe to send.
    """
    if exc.__cause__ is None:
        return str(exc)
    return hide_backend_text(fallback, exc, event=event, **fields)


def no_schema_error(source: str, version_id: str) -> ProblemError:
    """Build the 404 for a source version that names no schema, saying how to give it one."""
    return ProblemError(
        404,
        f"Source {source!r} version {version_id!r} has no schema configured. "
        "Set schema.meta_schema, schema.derived_schema or schema.additional_fields "
        "on the source, importing a meta schema with POST /api/v1/schemas/import "
        "first if none fits",
        code="no_schema",
    )


def raise_exchange_http(exc: Exception) -> NoReturn:
    """Map an import/export failure to its HTTP answer.

    A conflict is 409, matching the core-resource guard's answer elsewhere. A
    reference this deployment cannot resolve is its own code, because the fix is
    to move dfe-schemas rather than to edit the document.
    """
    from dfe_engine.exchange.schemas import ExchangeConflictError, ExchangeUnresolvedError

    if isinstance(exc, ExchangeConflictError):
        raise ProblemError(409, str(exc), code=ErrorCode.CONFLICT) from exc
    if isinstance(exc, ExchangeUnresolvedError):
        raise ProblemError(422, str(exc), code=ErrorCode.UNRESOLVED_REFERENCE) from exc
    raise ProblemError(422, str(exc), code=ErrorCode.VALIDATION_ERROR) from exc


# -- Exception handlers ---------------------------------------


def _answer(
    status: int, code: str, detail: str, headers: dict[str, str] | None = None, **extensions: Any
) -> JSONResponse:
    error = ProblemError(status, detail, code=code, headers=headers, **extensions)
    return problem_response(error.problem, error.headers)


def install_exception_handlers(app: FastAPI) -> None:
    """Answer the engine's domain exceptions as problems. Call it after :func:`scalo.api.install`."""
    from scalo.resilience import ServiceUnavailable

    from dfe_engine.api.password_floor import count_floor_refusal, is_floor_refusal
    from dfe_engine.api.task_manager import TaskCapacityError
    from dfe_engine.api.v1.scim import SCIM_ROOT, scim_error
    from dfe_engine.appmgmt.contract import RefusedNullError
    from dfe_engine.auth.models import AuthenticationError, AuthorizationError
    from dfe_engine.auth.protected_accounts import ProtectedAccountError
    from dfe_engine.gitcrud.engine import ResourceNotFoundError
    from dfe_engine.gitops.repo import GitopsUnavailableError
    from dfe_engine.names import InvalidNameError
    from dfe_engine.secrets import SecretNameHeldError

    def is_scim(request: Request) -> bool:
        return request.url.path.startswith(SCIM_ROOT)

    problem_validation = app.exception_handlers[RequestValidationError]

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, exc: RequestValidationError):
        if is_floor_refusal(exc.errors()):
            count_floor_refusal(request)
        return await problem_validation(request, exc)  # ty: ignore[invalid-await]  # scalo registers an async handler

    @app.exception_handler(AuthenticationError)
    async def unauthenticated(_request: Request, exc: AuthenticationError):
        return _answer(401, ErrorCode.UNAUTHORIZED, str(exc))

    @app.exception_handler(AuthorizationError)
    async def forbidden(request: Request, exc: AuthorizationError):
        if is_scim(request):
            return scim_error(403, str(exc))
        return _answer(403, ErrorCode.FORBIDDEN, str(exc))

    # Starlette matches the most derived handler, so this wins over AuthorizationError.
    @app.exception_handler(ProtectedAccountError)
    async def protected_account(request: Request, exc: ProtectedAccountError):
        if is_scim(request):
            # mutability is the nearest RFC 7644 scimType: the attribute is not
            # mutable on this resource, whatever it is on any other.
            return scim_error(403, str(exc), "mutability")
        return _answer(403, ErrorCode.PROTECTED_ACCOUNT, str(exc))

    @app.exception_handler(TaskCapacityError)
    async def task_capacity(_request: Request, exc: TaskCapacityError):
        return _answer(429, ErrorCode.TOO_MANY_TASKS, str(exc))

    # ``waking`` is true while a ClickHouse Cloud auto-wake is in flight, so the
    # console can say "warming up" and back off rather than report an outage.
    @app.exception_handler(ServiceUnavailable)
    async def service_unavailable(request: Request, exc: ServiceUnavailable):
        waking = bool(getattr(exc, "waking", False))
        message = "Backing service is warming up, retry shortly"
        if not waking:
            # scalo's message ends with the backend's last error, verbatim.
            message = hide_backend_text(
                SERVICE_UNAVAILABLE_MESSAGE,
                exc,
                event="backing service unavailable",
                route=route_template(request.scope),
            )
        return _answer(503, ErrorCode.SERVICE_UNAVAILABLE, message, waking=waking)

    # Starlette matches the most derived handler, so this wins over ServiceUnavailable.
    @app.exception_handler(GitopsUnavailableError)
    async def gitops_unavailable(request: Request, exc: GitopsUnavailableError):
        message = hide_backend_text(
            GITOPS_UNAVAILABLE_MESSAGE,
            exc,
            event="deploy repo unavailable",
            route=route_template(request.scope),
        )
        headers = {"Retry-After": str(exc.retry_after_seconds)}
        return _answer(
            503,
            ErrorCode.SERVICE_UNAVAILABLE,
            message,
            headers,
            service="gitops",
            remote=exc.remote,
        )

    @app.exception_handler(SecretNameHeldError)
    async def secret_name_held(_request: Request, exc: SecretNameHeldError):
        return _answer(409, ErrorCode.CONFLICT, exc.message)

    @app.exception_handler(InvalidNameError)
    async def invalid_name(_request: Request, exc: InvalidNameError):
        return _answer(400, ErrorCode.INVALID_NAME, str(exc))

    # Any write path that reaches the deploy repo with a null the app refuses, named by path.
    @app.exception_handler(RefusedNullError)
    async def refused_null(_request: Request, exc: RefusedNullError):
        return _answer(400, exc.code, str(exc), paths=exc.paths)

    @app.exception_handler(ResourceNotFoundError)
    async def resource_not_found(_request: Request, _exc: ResourceNotFoundError):
        return _answer(404, ErrorCode.NOT_FOUND, "The resource does not exist")

    # Registered here, not by scalo, so a SCIM client gets the RFC 7644 envelope.
    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # The route template, since a path can carry a login or other identifier.
        logger.opt(exception=exc).error(
            "Unhandled exception", method=request.method, route=route_template(request.scope)
        )
        message = "An unexpected error occurred"
        if is_scim(request):
            return scim_error(500, message)
        return _answer(500, ErrorCode.INTERNAL_ERROR, message)


# -- OpenAPI declarations -------------------------------------

_UNAUTHORIZED_DESCRIPTION = (
    "No valid session: the credentials are missing, invalid or expired, or the "
    "account behind them is disabled or blocked"
)
_FORBIDDEN_DESCRIPTION = (
    "Refused: the session lacks the action this route checks, or its account must "
    "replace an issued password first. A SCIM route answers a refused action in the "
    "RFC 7644 error envelope"
)


def declare_auth_errors(openapi: dict[str, Any], routes: Sequence[BaseRoute]) -> None:
    """Declare 401 and 403 on every operation a session guards.

    Those are the routes whose dependency tree reaches
    :func:`~dfe_engine.api.deps.get_current_user`, through ``CurrentUser`` or
    ``require_action`` (:func:`~dfe_engine.api.deps.guarded_operations`). A
    public route carries neither. :func:`scalo.api.install` types both as problems.

    Args:
        openapi: The generated OpenAPI document, updated in place.
        routes: The application's routes, included routers unexpanded.
    """
    from dfe_engine.api.deps import guarded_operations

    for _path, operation in guarded_operations(openapi, routes):
        responses = operation.setdefault("responses", {})
        responses.setdefault("401", {"description": _UNAUTHORIZED_DESCRIPTION})
        responses.setdefault("403", {"description": _FORBIDDEN_DESCRIPTION})
        operation["responses"] = dict(sorted(responses.items()))
