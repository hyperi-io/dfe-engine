#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_error_log_paths.py
#  Purpose:      A 500 or 503 logs the route template, never a username in the request path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The error handlers log the matched route's template, not the path a caller sent.

Routes under the engine's own app raise what a handler meets in service: an
unexpected error (500), a backing service out of budget (503) and an unreachable
deploy repo (503). Each is requested with a login in the path. The sink formats the
traceback the way scalo's sinks do, with no locals, so the whole line is checked.
"""

from collections.abc import Generator
from contextlib import contextmanager

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from scalo.logger import logger
from scalo.resilience import ServiceUnavailable

from dfe_engine.gitops.repo import GitopsUnavailableError

_LOGIN = "jane.doe@example.com"
_LOCAL_PART = "jane.doe"
_TEMPLATE = "/api/v1/error-probe/{username}"


def _probe_router() -> APIRouter:
    router = APIRouter()

    @router.get("/{username}/boom")
    async def boom(username: str) -> None:
        raise RuntimeError("probe failure")

    @router.get("/{username}/backend")
    async def backend(username: str) -> None:
        raise ServiceUnavailable("ClickHouse unreachable after 2s")

    @router.get("/{username}/deploy-repo")
    async def deploy_repo(username: str) -> None:
        raise GitopsUnavailableError("remote did not answer")

    return router


@contextmanager
def every_line() -> Generator[list[str]]:
    """Each record written inside the block, with its fields and traceback, as scalo formats them."""
    lines: list[str] = []
    sink = logger.add(
        lines.append, level="DEBUG", format="{message} {extra}\n{exception}", diagnose=False
    )
    try:
        yield lines
    finally:
        logger.remove(sink)


@pytest.mark.parametrize(
    ("leaf", "status"),
    [("boom", 500), ("backend", 503), ("deploy-repo", 503)],
    ids=["unhandled-500", "backing-service-503", "deploy-repo-503"],
)
def test_the_error_line_names_the_route_and_never_the_login(app, leaf, status):
    app.include_router(_probe_router(), prefix="/api/v1/error-probe")
    with TestClient(app, raise_server_exceptions=False) as client, every_line() as lines:
        response = client.get(f"/api/v1/error-probe/{_LOGIN}/{leaf}")

    assert response.status_code == status
    assert any(f"{_TEMPLATE}/{leaf}" in line for line in lines), lines
    assert not [line for line in lines if _LOCAL_PART in line]
