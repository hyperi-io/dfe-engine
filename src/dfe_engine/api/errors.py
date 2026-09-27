"""Unified error handling for DFE Engine API.

All errors return ``ErrorResponse`` — a single shape the UI can parse uniformly.
Pydantic 422 errors are reshaped into the same format with field-level detail.
"""

from typing import Annotated, Any, Literal, NoReturn

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from scalo.logger import logger

# ── Response models ──────────────────────────────────────────


class FieldError(BaseModel):
    """A validation error on a specific field."""

    field: str = Field(description="Dotted path to the invalid field")
    message: str = Field(description="Human-readable error message")
    code: str = Field(default="validation_error", description="Machine-readable error code")


class ErrorResponse(BaseModel):
    """Unified error response.

    All API errors return this shape. The UI can always parse:
    - ``code`` for programmatic branching
    - ``message`` for a human-readable summary
    - ``errors`` for field-level validation details (422 only)
    - ``context`` for optional structured data on specific errors (e.g. conflicts)
    """

    code: str = Field(description="Machine-readable error code")
    message: str = Field(description="Human-readable error summary")
    errors: list[FieldError] = Field(default_factory=list)
    context: dict[str, Any] | None = Field(
        default=None,
        description="Structured details for specific errors (e.g. match_conflict)",
    )


class SourceNameConflictErrorResponse(BaseModel):
    """409 when POST /sources and the source name already exists."""

    code: Literal["conflict"] = "conflict"
    message: str = Field(description="Human-readable explanation")
    errors: list[FieldError] = Field(default_factory=list)


class MatchConflictContext(BaseModel):
    """Structured context for duplicate receiver match (field + value)."""

    source: str = Field(description="Source identifier being saved")
    conflicting_source: str = Field(
        description="Other enabled source that already uses this (field, value) pair",
    )
    field: str = Field(description="Receiver match JSON field name")
    value: str = Field(description="Receiver match expected value")


class MatchConflictErrorResponse(BaseModel):
    """409 when two enabled sources share the same receiver match rule."""

    code: Literal["match_conflict"] = "match_conflict"
    message: str = Field(description="Human-readable explanation")
    errors: list[FieldError] = Field(default_factory=list)
    context: MatchConflictContext


class CoreResourceConflictContext(BaseModel):
    """Structured context for a write that named an engine-owned resource."""

    source: str = Field(description="The engine-owned source the write named")


class CoreResourceConflictErrorResponse(BaseModel):
    """409 when a write names a resource the engine owns and reconciles itself."""

    code: Literal["conflict"] = "conflict"
    message: str = Field(description="Human-readable explanation")
    errors: list[FieldError] = Field(default_factory=list)
    context: CoreResourceConflictContext


# Plain union, not discriminated: SourceNameConflictErrorResponse and
# CoreResourceConflictErrorResponse both carry code "conflict", which
# Field(discriminator="code") refuses to build a union over.
SourceCreateConflictResponse = (
    SourceNameConflictErrorResponse | CoreResourceConflictErrorResponse | MatchConflictErrorResponse
)

SourceWriteConflictResponse = Annotated[
    MatchConflictErrorResponse | CoreResourceConflictErrorResponse,
    Field(discriminator="code"),
]


def _error_response_json(body: ErrorResponse) -> dict[str, Any]:
    """Serialize for HTTP; omit null optional fields."""
    return body.model_dump(mode="json", exclude_none=True)


# ── Error codes ──────────────────────────────────────────────


class ErrorCode:
    """Machine-readable error codes used in ErrorResponse."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PROTECTED_ACCOUNT = "protected_account"
    PASSWORD_CHANGE_REQUIRED = "password_change_required"
    INVALID_SQL = "invalid_sql"
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"
    UNRESOLVED_REFERENCE = "unresolved_reference"


def raise_exchange_http(exc: Exception) -> NoReturn:
    """Map an import/export failure to its HTTP answer.

    A conflict is 409, matching the core-resource guard's answer elsewhere. A
    reference this deployment cannot resolve is its own code, because the fix is
    to move dfe-schemas rather than to edit the document.
    """
    from dfe_engine.exchange.schemas import ExchangeConflictError, ExchangeUnresolvedError

    if isinstance(exc, ExchangeConflictError):
        raise HTTPException(
            status_code=409,
            detail={"code": ErrorCode.CONFLICT, "message": str(exc)},
        ) from exc
    if isinstance(exc, ExchangeUnresolvedError):
        raise HTTPException(
            status_code=422,
            detail={"code": ErrorCode.UNRESOLVED_REFERENCE, "message": str(exc)},
        ) from exc
    raise HTTPException(
        status_code=422,
        detail={"code": ErrorCode.VALIDATION_ERROR, "message": str(exc)},
    ) from exc


# ── Exception handlers ───────────────────────────────────────


def install_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the app."""
    from dfe_engine.api.password_floor import count_floor_refusal, is_floor_refusal

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException):
        if isinstance(exc.detail, str):
            body = ErrorResponse(code="request_error", message=exc.detail)
        elif isinstance(exc.detail, dict):
            d = exc.detail
            code = d.get("error") or d.get("code") or "request_error"
            if not isinstance(code, str):
                code = "request_error"
            message = d.get("message")
            if not isinstance(message, str):
                message = str(d)
            reserved = {"code", "message", "error"}
            ctx = {k: v for k, v in d.items() if k not in reserved}
            body = ErrorResponse(code=code, message=message, context=ctx or None)
        else:
            body = ErrorResponse(code="request_error", message=str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content=_error_response_json(body))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        if is_floor_refusal(errors):
            count_floor_refusal(request)
        field_errors = [
            FieldError(
                field=".".join(str(loc) for loc in err["loc"] if loc != "body"),
                message=err["msg"],
                code=err.get("type", "validation_error"),
            )
            for err in errors
        ]
        body = ErrorResponse(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"{len(field_errors)} validation error(s)",
            errors=field_errors,
        )
        return JSONResponse(status_code=422, content=_error_response_json(body))

    # Import engine exceptions here to avoid circular imports at module level
    from dfe_engine.auth.models import AuthenticationError, AuthorizationError

    @app.exception_handler(AuthenticationError)
    async def auth_error_handler(_request: Request, exc: AuthenticationError):
        body = ErrorResponse(code=ErrorCode.UNAUTHORIZED, message=str(exc))
        return JSONResponse(status_code=401, content=_error_response_json(body))

    @app.exception_handler(AuthorizationError)
    async def authz_error_handler(_request: Request, exc: AuthorizationError):
        body = ErrorResponse(code=ErrorCode.FORBIDDEN, message=str(exc))
        return JSONResponse(status_code=403, content=_error_response_json(body))

    # Registered once rather than caught per route, so a route nobody has written
    # yet answers a protected-name refusal the same way.
    # Starlette matches the most derived handler, so this wins over AuthorizationError.
    from dfe_engine.api.v1.scim import SCIM_ROOT, scim_error
    from dfe_engine.auth.protected_accounts import ProtectedAccountError

    @app.exception_handler(ProtectedAccountError)
    async def protected_account_handler(request: Request, exc: ProtectedAccountError):
        if request.url.path.startswith(SCIM_ROOT):
            # mutability is the nearest RFC 7644 scimType: the attribute is not
            # mutable on this resource, whatever it is on any other.
            return scim_error(403, str(exc), "mutability")
        body = ErrorResponse(code=ErrorCode.PROTECTED_ACCOUNT, message=str(exc))
        return JSONResponse(status_code=403, content=_error_response_json(body))

    # A backing service (today: ClickHouse) was unreachable after its
    # reconnect-and-retry budget was exhausted. Retryable -> 503, never a
    # 500/crash. ``waking`` is True when a CH Cloud auto-wake was in flight, so the
    # UI can say "warming up" and back off rather than treat it as a hard outage.
    from scalo.resilience import ServiceUnavailable

    @app.exception_handler(ServiceUnavailable)
    async def service_unavailable_handler(_request: Request, exc: ServiceUnavailable):
        waking = bool(getattr(exc, "waking", False))
        message = "Backing service is warming up, retry shortly" if waking else str(exc)
        body = ErrorResponse(
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message=message,
            context={"waking": waking},
        )
        return JSONResponse(status_code=503, content=_error_response_json(body))

    # The deploy repo stayed unreachable for a write's whole retry budget. Starlette
    # matches the most derived handler, so this wins over ServiceUnavailable.
    from dfe_engine.gitops.repo import GitopsUnavailableError

    @app.exception_handler(GitopsUnavailableError)
    async def gitops_unavailable_handler(_request: Request, exc: GitopsUnavailableError):
        body = ErrorResponse(
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message=str(exc),
            context={"service": "gitops", "remote": exc.remote},
        )
        return JSONResponse(
            status_code=503,
            content=_error_response_json(body),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception(f"Unhandled exception on {request.method} {request.url.path}")
        body = ErrorResponse(code=ErrorCode.INTERNAL_ERROR, message="An unexpected error occurred")
        return JSONResponse(status_code=500, content=_error_response_json(body))
