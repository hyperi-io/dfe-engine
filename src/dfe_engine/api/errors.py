"""Unified error handling for DFE Engine API.

All errors return ``ErrorResponse`` — a single shape the UI can parse uniformly.
Pydantic 422 errors are reshaped into the same format with field-level detail.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from hyperi_pylib.logger import logger
from pydantic import BaseModel, Field

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
    """

    code: str = Field(description="Machine-readable error code")
    message: str = Field(description="Human-readable error summary")
    errors: list[FieldError] = Field(default_factory=list)


# ── Error codes ──────────────────────────────────────────────


class ErrorCode:
    """Machine-readable error codes used in ErrorResponse."""

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID_SQL = "invalid_sql"
    INTERNAL_ERROR = "internal_error"


# ── Exception handlers ───────────────────────────────────────


def install_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the app."""

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException):
        if isinstance(exc.detail, str):
            body = ErrorResponse(code="request_error", message=exc.detail)
        elif isinstance(exc.detail, dict):
            body = ErrorResponse(
                code=exc.detail.get("error", exc.detail.get("code", "request_error")),
                message=exc.detail.get("message", str(exc.detail)),
            )
        else:
            body = ErrorResponse(code="request_error", message=str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, exc: RequestValidationError):
        field_errors = [
            FieldError(
                field=".".join(str(loc) for loc in err["loc"] if loc != "body"),
                message=err["msg"],
                code=err.get("type", "validation_error"),
            )
            for err in exc.errors()
        ]
        body = ErrorResponse(
            code=ErrorCode.VALIDATION_ERROR,
            message=f"{len(field_errors)} validation error(s)",
            errors=field_errors,
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    # Import engine exceptions here to avoid circular imports at module level
    from dfe_engine.auth.models import AuthenticationError, AuthorizationError

    @app.exception_handler(AuthenticationError)
    async def auth_error_handler(_request: Request, exc: AuthenticationError):
        body = ErrorResponse(code=ErrorCode.UNAUTHORIZED, message=str(exc))
        return JSONResponse(status_code=401, content=body.model_dump(mode="json"))

    @app.exception_handler(AuthorizationError)
    async def authz_error_handler(_request: Request, exc: AuthorizationError):
        body = ErrorResponse(code=ErrorCode.FORBIDDEN, message=str(exc))
        return JSONResponse(status_code=403, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception(f"Unhandled exception on {request.method} {request.url.path}")
        body = ErrorResponse(code=ErrorCode.INTERNAL_ERROR, message="An unexpected error occurred")
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))
