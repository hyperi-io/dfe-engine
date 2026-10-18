#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_access_log.py
#  Purpose:      The API access line names the route and status, never a username in the path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The access line at INFO carries the route template and the status, not the path's values.

Once through the engine's app, against an account route whose path is the user's
login, and once through a real uvicorn server on the daemon's config, which turns
uvicorn's path-carrying access line off.
"""

from collections.abc import Generator
from contextlib import contextmanager

import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from scalo.logger import logger
from scalo.metrics.http_server import UNMATCHED_ROUTE

from dfe_engine.api.middleware.access_log import ACCESS_LINE, AccessLogMiddleware
from dfe_engine.api.server import server_config
from tests.support.uvicorn_access import serve_once, uvicorn_access_lines

_LOGIN = "jane.doe@example.com"
_LOCAL_PART = "jane.doe"


@contextmanager
def access_lines() -> Generator[list[dict]]:
    """The engine's access lines written inside the block, as their level and fields."""
    records: list[dict] = []

    def keep(message) -> None:
        record = message.record
        if record["message"] == ACCESS_LINE:
            records.append({"level": record["level"].name, **record["extra"]})

    sink = logger.add(keep, level="DEBUG")
    try:
        yield records
    finally:
        logger.remove(sink)


def test_an_account_lookup_logs_its_route_and_status_without_the_login(client, admin_headers):
    with access_lines() as lines:
        response = client.get(f"/api/v1/auth/accounts/{_LOGIN}", headers=admin_headers)

    assert response.status_code == 404
    assert lines == [
        {
            "level": "INFO",
            "method": "GET",
            "route": "/api/v1/auth/accounts/{username}",
            "status": 404,
            "duration_ms": lines[0]["duration_ms"],
            "client": lines[0]["client"],
        }
    ]
    assert _LOCAL_PART not in repr(lines)


def test_a_path_no_route_matches_is_logged_as_unmatched(client, admin_headers):
    with access_lines() as lines:
        client.get(f"/api/v1/no-such-route/{_LOGIN}", headers=admin_headers)

    assert [(line["route"], line["status"]) for line in lines] == [(UNMATCHED_ROUTE, 404)]
    assert _LOCAL_PART not in repr(lines)


_TENANT = "acme-corp"


def _users_router() -> APIRouter:
    router = APIRouter()

    @router.get("/users/{username}")
    async def user(username: str) -> dict[str, int]:
        return {"length": len(username)}

    return router


def _routes_logged(app: FastAPI, path: str) -> list[str]:
    app.add_middleware(AccessLogMiddleware)
    with access_lines() as lines, TestClient(app) as test_client:
        assert test_client.get(path).status_code == 200
    return [line["route"] for line in lines]


def test_a_parameter_in_an_include_prefix_never_puts_its_value_in_the_line():
    app = FastAPI()
    app.include_router(_users_router(), prefix="/tenants/{tenant}")

    routes = _routes_logged(app, f"/tenants/{_TENANT}/users/{_LOGIN}")

    assert routes == ["/users/{username}"]


def test_a_parameter_in_a_mount_path_never_puts_its_value_in_the_line():
    mounted = FastAPI()
    mounted.include_router(_users_router())
    app = FastAPI()
    app.mount("/orgs/{org}", mounted)

    routes = _routes_logged(app, f"/orgs/{_TENANT}/users/{_LOGIN}")

    assert len(routes) == 1
    assert _TENANT not in routes[0]
    assert _LOCAL_PART not in routes[0]


def test_a_prefix_with_no_parameter_is_kept_whole():
    app = FastAPI()
    app.include_router(_users_router(), prefix="/api/v1")

    assert _routes_logged(app, f"/api/v1/users/{_LOGIN}") == ["/api/v1/users/{username}"]


def _accounts_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/auth/accounts/{username}")
    async def account(username: str) -> dict[str, int]:
        return {"length": len(username)}

    app.add_middleware(AccessLogMiddleware)
    return app


def _serve_one_lookup(config_for) -> tuple[int, list[str], list[dict]]:
    """Serve the accounts app on *config_for*'s config and look one login up.

    Returns:
        The status, uvicorn's access lines and the engine's.
    """
    with uvicorn_access_lines() as uvicorn_lines, access_lines() as lines:
        status = serve_once(
            lambda port: config_for(_accounts_app(), port), f"/api/v1/auth/accounts/{_LOGIN}"
        )
    return status, uvicorn_lines, lines


def test_the_daemon_config_writes_no_uvicorn_access_line_with_the_path():
    status, uvicorn_lines, lines = _serve_one_lookup(
        lambda app, port: server_config(app, host="127.0.0.1", port=port)
    )

    assert status == 200
    assert uvicorn_lines == []
    assert [(line["route"], line["status"]) for line in lines] == [
        ("/api/v1/auth/accounts/{username}", 200)
    ]
    assert _LOCAL_PART not in repr(lines)


def test_uvicorns_own_access_line_carries_the_login_it_is_turned_off_for():
    """The control: the same server with uvicorn's access line on writes the path whole."""
    status, uvicorn_lines, _lines = _serve_one_lookup(
        lambda app, port: uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)
    )

    assert status == 200
    assert len(uvicorn_lines) == 1
    assert _LOCAL_PART in uvicorn_lines[0]
