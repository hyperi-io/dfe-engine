"""Every operation a session guards declares the token it needs and the 401 and 403 it answers.

dfe-ui generates its client from ``openapi-spec/openapi.json``, so an error the
spec leaves out has no type there, and a mock server built from it demands a
token wherever the spec says one is needed. The app derives both from each
route's dependency tree. The public operations are listed here by hand on
purpose: a test that asked the same tree would agree with it whatever it got
wrong, and a route that opens without a session deserves the edit below.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import CurrentUser, get_current_user, require_action
from dfe_engine.api.errors import declare_auth_errors
from dfe_engine.api.v1.scim import SCIM_MEDIA_TYPE, SCIM_ROOT
from dfe_engine.settings import load_settings, reset_settings

PUBLIC_OPERATIONS = frozenset(
    {
        "POST /api/v1/auth/login",
        "GET /api/v1/auth/setup-status",
        "GET /api/v1/auth/oidc/{provider}/login",
        "GET /api/v1/auth/oidc/{provider}/callback",
        "GET /api/v1/config/client",
        "GET /api/v1/scim/v2/ServiceProviderConfig",
        "GET /api/v1/scim/v2/ResourceTypes",
        "GET /api/v1/scim/v2/Schemas",
    }
)

# Public, but it refuses a bad login itself, so it declares both statuses by hand.
LOGIN = "POST /api/v1/auth/login"

AUTH_STATUSES = ("401", "403")
ERROR_SCHEMA = {"$ref": "#/components/schemas/ErrorResponse"}
BEARER = [{"BearerAuth": []}]


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def spec(monkeypatch) -> dict[str, Any]:
    """The product spec, built exactly as ``openapi-spec/generate.py`` builds it."""
    monkeypatch.setenv("DFE_ENV", "dev")
    settings = load_settings()
    if settings.e2e_server:
        settings = settings.model_copy(update={"e2e_server": False})
    app = create_app(settings=settings)
    app.openapi_schema = None
    return app.openapi()


def _operations(spec: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            yield f"{method.upper()} {path}", operation


def _json_schema(response: dict[str, Any]) -> Any:
    return response.get("content", {}).get("application/json", {}).get("schema")


def test_every_guarded_operation_declares_401_and_403(spec):
    """A client generated from the spec has no type for an error left out of it."""
    missing = [
        f"{key} -> {status}"
        for key, operation in _operations(spec)
        if key not in PUBLIC_OPERATIONS
        for status in AUTH_STATUSES
        if _json_schema(operation.get("responses", {}).get(status, {})) != ERROR_SCHEMA
    ]

    assert not missing, (
        f"{len(missing)} guarded operation(s) do not declare the ErrorResponse they "
        f"answer. A route that is public belongs in PUBLIC_OPERATIONS:\n  " + "\n  ".join(missing)
    )


def test_every_public_operation_is_real_and_declares_neither(spec):
    """A stale entry would excuse a guarded route that took over its path."""
    operations = dict(_operations(spec))

    assert PUBLIC_OPERATIONS <= operations.keys(), sorted(PUBLIC_OPERATIONS - operations.keys())
    declared = sorted(
        f"{key} -> {status}"
        for key in PUBLIC_OPERATIONS - {LOGIN}
        for status in AUTH_STATUSES
        if status in operations[key].get("responses", {})
    )
    assert not declared


def test_login_declares_the_401_and_403_it_answers(spec):
    """A wrong password is a 401 and a disabled break-glass account a 403, both native."""
    responses = dict(_operations(spec))[LOGIN]["responses"]

    for status in AUTH_STATUSES:
        assert _json_schema(responses.get(status, {})) == ERROR_SCHEMA, status


def test_only_a_guarded_operation_requires_the_token(spec):
    """A public operation needs no token, so a mock built from the spec must not demand one."""
    wrong = sorted(
        key
        for key, operation in _operations(spec)
        if operation.get("security") != (None if key in PUBLIC_OPERATIONS else BEARER)
    )

    assert not wrong, (
        f"{len(wrong)} operation(s) state the wrong security requirement: public ones "
        f"state none, guarded ones {BEARER}:\n  " + "\n  ".join(wrong)
    )


def test_a_scim_403_also_names_the_scim_envelope(spec):
    """A refused action on a SCIM route answers in RFC 7644's envelope, not the native one."""
    scim = [
        operation
        for key, operation in _operations(spec)
        if key.split(" ", 1)[1].startswith(SCIM_ROOT) and key not in PUBLIC_OPERATIONS
    ]

    assert scim
    for operation in scim:
        assert SCIM_MEDIA_TYPE in operation["responses"]["403"]["content"]
        assert SCIM_MEDIA_TYPE not in operation["responses"]["401"]["content"]


def _app_with_every_kind_of_guard() -> FastAPI:
    """Routes guarded at the parameter, the action and the include, beside an open one."""
    routes = APIRouter()

    @routes.get("/by-user")
    async def by_user(user: CurrentUser) -> None:
        return None

    @routes.get("/by-action", dependencies=[Depends(require_action("source:read"))])
    async def by_action() -> None:
        return None

    @routes.get("/open")
    async def open_route() -> None:
        return None

    fenced = APIRouter()

    @fenced.get("/fenced")
    async def fenced_route() -> None:
        return None

    outer = APIRouter(prefix="/outer")
    outer.include_router(routes)
    outer.include_router(fenced, dependencies=[Depends(get_current_user)])
    app = FastAPI()
    app.include_router(outer, prefix="/api")
    return app


def test_the_declaration_follows_the_dependency_tree():
    """Guarded however the guard is attached, and never on the open route."""
    app = _app_with_every_kind_of_guard()
    spec = app.openapi()

    declare_auth_errors(spec, app.routes)

    declared = {
        path
        for path, item in spec["paths"].items()
        if all(_json_schema(item["get"]["responses"].get(s, {})) for s in AUTH_STATUSES)
    }
    assert declared == {"/api/outer/by-user", "/api/outer/by-action", "/api/outer/fenced"}
    assert not set(AUTH_STATUSES) & spec["paths"]["/api/outer/open"]["get"]["responses"].keys()


def test_the_error_model_is_registered_where_no_route_declares_it():
    """The declared reference must resolve even when nothing else in the spec uses it."""
    app = _app_with_every_kind_of_guard()
    spec = app.openapi()
    assert "ErrorResponse" not in spec.get("components", {}).get("schemas", {})

    declare_auth_errors(spec, app.routes)

    schemas = spec["components"]["schemas"]
    assert {"ErrorResponse", "FieldError"} <= schemas.keys()
    assert list(schemas) == sorted(schemas)
