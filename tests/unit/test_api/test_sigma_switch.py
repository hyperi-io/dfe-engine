#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sigma_switch.py
#  Purpose:      The sigma API mounts only on a deployment that sets sigma.enabled
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The /api/v1/sigma router mounts only when ``sigma.enabled`` is set.

Off, every sigma path answers 404 while the field-map API that ECS, CIM and OCSF
share keeps answering. The daemon builds its settings from the environment with no
config file, so ``DFE_SIGMA_ENABLED`` is the switch a deployment actually reaches.
"""

import re

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from dfe_engine.api.app import create_app
from dfe_engine.api.v1.sigma import router as sigma_router
from dfe_engine.settings import DFESettings, FieldMapSettings, load_settings

SIGMA_PREFIX = "/api/v1/sigma"
SIGMA_ROUTES = [route for route in sigma_router.routes if isinstance(route, APIRoute)]
SIGMA_PATHS = {"/api/v1" + route.path for route in SIGMA_ROUTES}


@pytest.fixture
def api_settings(api_settings: DFESettings, tmp_path) -> DFESettings:
    """The shared settings plus a field-map store, so the field-map API answers 200."""
    fieldmaps_dir = tmp_path / "fieldmaps"
    fieldmaps_dir.mkdir()
    return api_settings.model_copy(
        update={"fieldmap": FieldMapSettings(fieldmaps_dir=str(fieldmaps_dir))}
    )


def _mounted_paths(app) -> set[str]:
    # Included routers stay nested in app.routes, so each route is read as mounted.
    return {route.path for route in iter_route_contexts(app.routes) if route.path}


def _every_sigma_request() -> list[tuple[str, str]]:
    """Each method and path the sigma router serves, its path parameters filled in."""
    requests = []
    for route in SIGMA_ROUTES:
        path = "/api/v1" + re.sub(r"\{[^}]+\}", "x", route.path)
        requests.extend((method, path) for method in sorted(route.methods or ()))
    return requests


def test_the_sigma_router_lives_under_its_prefix():
    assert len(SIGMA_ROUTES) == len(sigma_router.routes)
    assert SIGMA_PATHS
    assert all(path.startswith(f"{SIGMA_PREFIX}/") for path in SIGMA_PATHS)


def test_no_sigma_route_is_mounted_by_default(app):
    mounted = _mounted_paths(app)

    assert "/api/v1/sources" in mounted
    assert not {path for path in mounted if path.startswith(SIGMA_PREFIX)}


def test_every_sigma_path_answers_404_by_default(client, admin_headers):
    answered = {
        (method, path): client.request(method, path, headers=admin_headers).status_code
        for method, path in _every_sigma_request()
    }

    assert set(answered.values()) == {404}, answered


def test_the_field_map_api_stays_mounted_with_sigma_off(app, client, admin_headers):
    assert "/api/v1/field-maps" in _mounted_paths(app)
    assert client.get("/api/v1/field-maps", headers=admin_headers).status_code == 200


@pytest.mark.parametrize("sigma_enabled", [True])
def test_enabling_sigma_mounts_every_sigma_route(app, client):
    mounted = _mounted_paths(app)

    assert {path for path in mounted if path.startswith(SIGMA_PREFIX)} == SIGMA_PATHS
    # Mounted behind the same auth as every other route, so an anonymous caller is refused.
    assert client.get(f"{SIGMA_PREFIX}/views").status_code == 401


@pytest.mark.parametrize(("value", "mounted"), [("true", True), ("false", False)])
def test_the_environment_switch_reaches_the_app(monkeypatch, value, mounted):
    """Built the way the daemon builds it: load_settings() with no file, then create_app."""
    monkeypatch.setenv("DFE_ENV", "dev")
    monkeypatch.setenv("DFE_SIGMA_ENABLED", value)

    mounted_paths = _mounted_paths(create_app(settings=load_settings()))

    sigma = {path for path in mounted_paths if path.startswith(SIGMA_PREFIX)}
    assert sigma == (SIGMA_PATHS if mounted else set())
    assert "/api/v1/field-maps" in mounted_paths
