#  Project:      dfe-engine
#  File:         api/middleware/access_log.py
#  Purpose:      One access line per HTTP request, naming the route and never its values
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The API's access line, written with the matched route's template in place of the path.

A request path can carry a value, the user's login in
``/api/v1/auth/accounts/{username}`` for one. The template keeps the shape of the
request without its values. scalo's request histogram labels the same full template,
except above a parameter: it names a parameterised mount from its template and labels a
parameterised router prefix ``__unresolved__``, where this line keeps the route's own.
"""

import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from scalo.logger import logger
from scalo.metrics.http_server import DEFAULT_SKIP_PATHS, UNMATCHED_ROUTE
from starlette.routing import replace_params

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

ACCESS_LINE = "HTTP request served"


def route_template(scope: Scope) -> str:
    """The matched route's full template, ``UNMATCHED_ROUTE`` when nothing matched.

    FastAPI matches an included router lazily, so the route on the scope carries only
    its own router's prefix. The prefixes above it are taken from the request path, as
    the part before the route rendered with the matched values. That part is used only
    when it holds none of the matched values: a parameter in a router prefix or a mount
    path, ``/tenants/{tenant}``, would otherwise put its value in the result. In that
    case, and wherever the render does not line up, the route's own template stands.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    if not isinstance(template, str):
        return UNMATCHED_ROUTE
    matched = dict(scope.get("path_params") or {})
    try:
        rendered, _unused = replace_params(
            template, getattr(route, "param_convertors", {}), dict(matched)
        )
    except KeyError, TypeError, ValueError, AssertionError:
        return template
    path = str(scope.get("path", ""))
    if not rendered or not path.endswith(rendered):
        return template
    prefix = path[: len(path) - len(rendered)]
    values = (str(value) for value in matched.values())
    if any(value and value in prefix for value in values):
        return template
    return prefix + template


class AccessLogMiddleware:
    """Pure ASGI middleware writing one INFO line per HTTP request once it has been answered.

    The line carries the method, the route template (``UNMATCHED_ROUTE`` when no route
    matched), the status, the time taken and the client address. Probe and scrape
    paths are not logged.

    Args:
        app: The ASGI app to wrap.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        """Wrap *app*."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the request through the app, then write its access line."""
        if scope["type"] != "http" or scope["path"] in DEFAULT_SKIP_PATHS:
            await self._app(scope, receive, send)
            return

        # A handler that raises before responding is logged as the 500 the server sends.
        status = {"code": 500}

        async def recording_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        started = time.perf_counter()
        try:
            await self._app(scope, receive, recording_send)
        finally:
            client = scope.get("client")
            logger.info(
                ACCESS_LINE,
                method=scope.get("method", ""),
                route=route_template(scope),
                status=status["code"],
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                client=client[0] if client else "",
            )
