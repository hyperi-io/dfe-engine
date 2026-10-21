"""The route map generator refuses a plan that disagrees with the app or the hierarchy.

``test_openapi_spec_committed.py`` holds the committed page to the app. These tests
break one input at a time and check that the generator names it, and that a
consumer's path resolves to the route it calls.
"""

from types import ModuleType

import pytest
from common.openapi_tools import load_route_map

from dfe_engine.api.app import create_app
from dfe_engine.api.conventions import protocol_paths
from dfe_engine.settings import SigmaSettings, load_settings, reset_settings

TEMPLATES = (
    "/api/v1/sources",
    "/api/v1/sources/catalogue",
    "/api/v1/sources/{name}",
    "/api/v1/sources/{name}/deploy",
)


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def route_map() -> ModuleType:
    return load_route_map()


def _row(route_map, old: str, status: str, new: str | None = None, note: str | None = None):
    method, path = old.split(" ", 1)
    new_method, new_path = new.split(" ", 1) if new else (method, path)
    return route_map.Row(method, path, status, new_method, new_path, None, note, False)


def test_a_served_operation_with_no_row_is_refused(route_map):
    with pytest.raises(route_map.RouteMapError, match="the plan has no row for it"):
        route_map.check_plan([], {"GET /api/v1/sources"})


def test_a_row_for_an_operation_nobody_serves_is_refused(route_map):
    rows = [_row(route_map, "GET /api/v1/gone", "decide", note="q")]

    with pytest.raises(route_map.RouteMapError, match="neither it nor its new path is served"):
        route_map.check_plan(rows, set())


@pytest.mark.parametrize(
    ("new", "refusal"),
    [
        ("GET /api/v1/source", "not plural"),
        ("GET /api/v1/widgets", "not a declared root"),
        ("GET /api/v1/sources/{source}/rules/{rule}/parts/{part}/bits/{bit}", "over 3"),
    ],
)
def test_a_new_path_the_hierarchy_refuses_is_refused(route_map, new, refusal):
    rows = [_row(route_map, "GET /api/v1/sources", "moved", new)]

    with pytest.raises(route_map.RouteMapError, match=refusal):
        route_map.check_plan(rows, {"GET /api/v1/sources"})


def test_two_rows_moving_to_one_operation_are_refused(route_map):
    rows = [
        _row(route_map, "GET /api/v1/a", "moved", "GET /api/v1/sources"),
        _row(route_map, "GET /api/v1/b", "moved", "GET /api/v1/sources"),
    ]

    with pytest.raises(route_map.RouteMapError, match=r"sources\.list: the target of"):
        route_map.check_plan(rows, {"GET /api/v1/a", "GET /api/v1/b"})


def test_a_merge_into_an_operation_no_row_produces_is_refused(route_map):
    rows = [_row(route_map, "POST /api/v1/sources", "merged", "PUT /api/v1/sources/{source}")]

    with pytest.raises(route_map.RouteMapError, match="no moved or unchanged row produces"):
        route_map.check_plan(rows, {"POST /api/v1/sources"})


def test_a_row_whose_route_has_already_moved_passes(route_map):
    rows = [_row(route_map, "GET /api/v1/kafka/topics", "moved", "GET /api/v1/deployment/topics")]

    route_map.check_plan(rows, {"GET /api/v1/deployment/topics"})


def test_the_committed_plan_freezes_exactly_the_protocol_routers(route_map, monkeypatch):
    """The plan's protocol rows and the conformance exemptions are one list, read twice."""
    monkeypatch.setenv("DFE_ENV", "dev")
    settings = load_settings().model_copy(
        update={"e2e_server": False, "sigma": SigmaSettings(enabled=True)}
    )
    app = create_app(settings=settings)
    rows, _ = route_map.load_plan()

    frozen = {row.path for row in rows if row.note == "protocol"}

    assert frozen == set(protocol_paths(app.routes))


@pytest.mark.parametrize(
    ("text", "named"),
    [
        (
            "fetch(`/api/v1/sources/${encodeURIComponent(name)}/deploy`)",
            "/api/v1/sources/{name}/deploy",
        ),
        ("client.get('/api/v1/sources/catalogue')", "/api/v1/sources/catalogue"),
        ('client.get("/api/v1/sources/{name}", pathParams)', "/api/v1/sources/{name}"),
        ('f"/api/v1/sources/{source}"', "/api/v1/sources/{name}"),
        ("'/api/v1/sources/' + name", "/api/v1/sources/{name}"),
        ("curl $ENGINE/api/v1/sources?page_size=5", "/api/v1/sources"),
    ],
)
def test_a_consumer_path_resolves_to_the_route_it_calls(route_map, text, named):
    templates = [(path, route_map.template_parts(path)) for path in TEMPLATES]

    found, unmatched = route_map.paths_named(text, templates)

    assert (found, unmatched) == ({named}, set())


def test_another_services_path_matches_no_route(route_map):
    templates = [(path, route_map.template_parts(path)) for path in TEMPLATES]

    found, unmatched = route_map.paths_named('"/api/v1/namespaces/dfe/pods"', templates)

    assert (found, unmatched) == (set(), {"/api/v1/namespaces/dfe/pods"})
