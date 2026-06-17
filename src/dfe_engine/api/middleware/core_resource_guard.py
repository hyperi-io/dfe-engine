#  Purpose:      HTTP middleware blocking mutations on core resources
#  License:      BUSL-1.1

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from dfe_engine.api.deps import _registries
from dfe_engine.api.errors import ErrorCode, ErrorResponse, _error_response_json
from dfe_engine.core_resources.policy import MUTATION_METHODS, match_api_core_mutation
from dfe_engine.core_resources.yaml_resource_type import CORE_RESOURCE_MUTATION_MESSAGE


def _core_mutation_error_response() -> Response:
    payload = _error_response_json(
        ErrorResponse(
            code=ErrorCode.CONFLICT,
            message=CORE_RESOURCE_MUTATION_MESSAGE,
        )
    )
    return Response(
        content=json.dumps(payload).encode("utf-8"),
        status_code=409,
        media_type="application/json",
    )


async def core_resource_guard_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Block API writes to YAML resources with ``resource_type: core``."""
    method = request.method.upper()
    path = request.url.path

    request_body = b""
    if method in MUTATION_METHODS and path.startswith("/api/"):
        request_body = await request.body()

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": request_body, "more_body": False}

        request = Request(request.scope, receive)

    if match_api_core_mutation(
        method=method,
        path=path,
        role_store=getattr(request.app.state, "role_store", None),
        schema_registry=_registries.get("meta_schema"),
        fieldmap_registry=_registries.get("fieldmap"),
        body=request_body,
    ):
        return _core_mutation_error_response()

    return await call_next(request)


def install_core_resource_guard(app: FastAPI) -> None:
    """Register core-resource guard (call before CORS ``add_middleware``)."""
    app.middleware("http")(core_resource_guard_middleware)
