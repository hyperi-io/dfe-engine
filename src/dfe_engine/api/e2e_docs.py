#  Project:      dfe-engine
#  File:         api/e2e_docs.py
#  Purpose:      Split Swagger into API vs E2E specs with a select-box dropdown
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Swagger UI spec selector for ``make e2e-server``.

When e2e routes are mounted, the product API and the Playwright helpers are
two OpenAPI documents. ``/docs`` loads Swagger UI's ``urls`` dropdown so the
operator picks **API** or **E2E** instead of seeing both in one page.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse

E2E_OPENAPI_PATH = "/openapi.e2e.json"
E2E_PATH_PREFIX = "/api/e2e"
E2E_TAG_NAME = "E2E"

_SWAGGER_JS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
_SWAGGER_PRESET_JS = (
    "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"
)
_SWAGGER_CSS = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"
_SWAGGER_FAVICON = "https://fastapi.tiangolo.com/img/favicon.png"


def split_openapi(full: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(product API spec, e2e-only spec)`` from a combined schema."""
    api = copy.deepcopy(full)
    e2e = copy.deepcopy(full)
    paths = full.get("paths", {})
    api["paths"] = {
        path: item for path, item in paths.items() if not path.startswith(E2E_PATH_PREFIX)
    }
    e2e["paths"] = {path: item for path, item in paths.items() if path.startswith(E2E_PATH_PREFIX)}

    tags = full.get("tags", [])
    api_tags = [tag for tag in tags if tag.get("name") != E2E_TAG_NAME]
    if api_tags:
        api["tags"] = api_tags
    else:
        api.pop("tags", None)
    e2e["tags"] = [tag for tag in tags if tag.get("name") == E2E_TAG_NAME]

    info = dict(e2e.get("info", {}))
    info["title"] = f"{info.get('title', 'DFE Engine API')} — E2E"
    e2e["info"] = info
    return api, e2e


def build_e2e_spec(*, version: str) -> dict[str, Any]:
    """OpenAPI document for the Playwright helpers only — no product API paths."""
    from dfe_engine.api.e2e import E2E_OPENAPI_TAG
    from dfe_engine.api.e2e import router as e2e_router

    app = FastAPI(
        title="DFE Engine API — E2E",
        description=(
            "Playwright helpers for `make e2e-server`. Unauthenticated. "
            "Mounted and documented only in that mode — not in the product spec."
        ),
        version=version,
        openapi_tags=[E2E_OPENAPI_TAG],
    )
    app.include_router(e2e_router, prefix="/api")
    return get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )


def swagger_select_html(*, title: str) -> HTMLResponse:
    """Swagger UI with a top-bar select for the API spec vs the E2E spec."""
    config = {
        "urls": [
            {"url": "/openapi.json", "name": "API"},
            {"url": E2E_OPENAPI_PATH, "name": "E2E"},
        ],
        "urls.primaryName": "API",
        "dom_id": "#swagger-ui",
        "layout": "StandaloneLayout",
        "deepLinking": True,
        "showExtensions": True,
        "showCommonExtensions": True,
        "validatorUrl": None,
    }
    config_js = json.dumps(jsonable_encoder(config)).replace("<", "\\u003c").replace(">", "\\u003e")
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" type="text/css" href="{_SWAGGER_CSS}">
<link rel="shortcut icon" href="{_SWAGGER_FAVICON}">
<title>{title}</title>
</head>
<body>
<div id="swagger-ui"></div>
<script src="{_SWAGGER_JS}"></script>
<script src="{_SWAGGER_PRESET_JS}"></script>
<script>
const ui = SwaggerUIBundle({{
  ...{config_js},
  presets: [
    SwaggerUIBundle.presets.apis,
    SwaggerUIStandalonePreset
  ],
}});
</script>
</body>
</html>
"""
    return HTMLResponse(html)


def install_e2e_swagger(app: FastAPI) -> None:
    """Serve ``/openapi.e2e.json`` and replace ``/docs`` with the spec selector."""

    @app.get(E2E_OPENAPI_PATH, include_in_schema=False)
    async def openapi_e2e() -> JSONResponse:
        return JSONResponse(build_e2e_spec(version=app.version))

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return swagger_select_html(title=f"{app.title} docs")
