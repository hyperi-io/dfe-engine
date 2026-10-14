#  Project:      dfe-engine
#  File:         api/body_limits.py
#  Purpose:      Refuse an oversized request body with 413 before it is buffered
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Bounded reads of a request body.

:class:`RequestBodyCapMiddleware` reads every request body before any other part of
the app does, refusing the declared Content-Length first and then a running total as
chunks arrive, so nothing downstream ever holds more than the limit. The limit is
``api.max_request_body_bytes``, except on a route that takes a file upload and names
its own bound with :func:`own_limit`.
"""

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.routing import iter_route_contexts
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dfe_engine.api.errors import ErrorResponse, _error_response_json
from dfe_engine.settings import DFESettings

TOO_LARGE_CODE = "payload_too_large"
"""The ``code`` of every 413 a capped body answers with."""

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_API_SETTING = "api.max_request_body_bytes"
_OWN_LIMIT = "__dfe_body_limit__"

type Bound = Callable[[DFESettings], int]
"""The most a route accepts, in bytes, read from the settings on each request."""


def too_large(code: str, message: str) -> HTTPException:
    """The 413 a capped route answers with."""
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={"code": code, "message": message},
    )


def refuse_declared_oversize(request: Request, *, limit: int, code: str, message: str) -> None:
    """Refuse a body whose declared Content-Length exceeds ``limit``, unread.

    A missing or malformed header is not a refusal: the read that follows still
    counts what actually arrives.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        declared = int(raw)
    except ValueError:
        return
    if declared > limit:
        raise too_large(code, message)


async def read_body_capped(request: Request, *, limit: int, code: str, message: str) -> bytes:
    """The request body, refused as soon as more than ``limit`` bytes have arrived."""
    refuse_declared_oversize(request, limit=limit, code=code, message=message)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise too_large(code, message)
        chunks.append(chunk)
    return b"".join(chunks)


def own_limit[F: Callable[..., Any]](setting: str, bound: Bound) -> Callable[[F], F]:
    """Bound a route that takes a file upload by its own setting instead of the API cap.

    Args:
        setting: The dotted setting an operator raises to accept a larger upload.
        bound: The most the route accepts, in bytes, given the settings.

    Returns:
        A decorator that marks the endpoint and returns it unchanged.
    """

    def mark(endpoint: F) -> F:
        setattr(endpoint, _OWN_LIMIT, (setting, bound))
        return endpoint

    return mark


def _over_limit_message(limit: int, setting: str) -> str:
    """What an operator reads when a body is refused, naming the limit and how to raise it."""
    env = "DFE_" + setting.replace(".", "_").upper()
    return (
        f"The request body is larger than the {limit}-byte limit. "
        f"Raise {setting} ({env}) to accept a larger one."
    )


def _body_limit(scope: Scope) -> tuple[int, str]:
    """The byte limit on this request's body, and the setting that raises it."""
    app = scope["app"]
    settings: DFESettings = app.state.settings
    # Included routers stay nested in app.routes, so each route is read as mounted.
    for route in iter_route_contexts(app.routes):
        own = getattr(route.endpoint, _OWN_LIMIT, None)
        if own is not None and route.matches(scope)[0] is Match.FULL:
            setting, bound = own
            return bound(settings), setting
    return settings.api.max_request_body_bytes, _API_SETTING


def _replay(body: bytes, receive: Receive) -> Receive:
    """A receive that hands ``body`` over once, then passes the client's messages on."""
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if delivered:
            return await receive()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


class RequestBodyCapMiddleware:
    """Answer 413 to a request body over its limit, before anything downstream buffers it.

    It sits outside the core-resource guard, which reads every write body whole, so it
    must run first. Its own response is built here because the app's exception
    handlers sit inside the middleware stack.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``, the rest of the middleware stack."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Read a write's body up to its limit and pass it on, or answer 413."""
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        limit, setting = _body_limit(scope)
        message = _over_limit_message(limit, setting)
        try:
            body = await read_body_capped(
                Request(scope, receive), limit=limit, code=TOO_LARGE_CODE, message=message
            )
        except HTTPException as exc:
            refusal = JSONResponse(
                status_code=exc.status_code,
                content=_error_response_json(ErrorResponse(code=TOO_LARGE_CODE, message=message)),
            )
            await refusal(scope, receive, send)
            return
        await self.app(scope, _replay(body, receive), send)
