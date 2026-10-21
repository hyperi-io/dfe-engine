"""The engine API is held to scalo.api's conventions, less the violations listed as data.

``tests/fixtures/api_conformance_allow.txt`` holds the violations the API carries
today, one ``<rule> <METHOD> <path>`` key a line. A new operation that breaks a rule
fails here, and so does a listed violation that no longer happens, so the list only
shrinks as each router moves into the resource hierarchy. The protocol routers are
exempt by name (``dfe_engine.api.conventions.PROTOCOL_ROUTERS``).

The app is built the way ``openapi-spec/generate.py`` builds it, sigma included, so
the check covers every operation the committed spec describes.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from scalo.api import EXTENSION, assert_conformant, check_conformance

from dfe_engine.api.app import create_app
from dfe_engine.api.conventions import HIERARCHY, protocol_paths
from dfe_engine.settings import SigmaSettings, load_settings, reset_settings

ALLOW_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "api_conformance_allow.txt"


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def spec_app(monkeypatch) -> FastAPI:
    monkeypatch.setenv("DFE_ENV", "dev")
    settings = load_settings().model_copy(
        update={"e2e_server": False, "sigma": SigmaSettings(enabled=True)}
    )
    return create_app(settings=settings)


def _allowed() -> list[str]:
    lines = [line.strip() for line in ALLOW_FILE.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def test_every_operation_meets_the_conventions_or_is_listed(spec_app):
    """Fails on a new violation, and on a listed one that is gone: trim the list then."""
    assert_conformant(spec_app, HIERARCHY, allow=_allowed(), exempt=protocol_paths(spec_app.routes))


def test_the_allow_list_names_each_violation_exactly():
    """A pattern entry would wave through a new route under an old one's name."""
    patterns = [entry for entry in _allowed() if any(char in entry for char in "*?[")]

    assert not patterns, patterns


def test_the_allow_list_is_sorted_and_unique():
    entries = _allowed()

    assert entries == sorted(set(entries))


def test_a_route_that_breaks_a_rule_fails_the_check(spec_app):
    """The check is live: drop one listed violation and it is reported, not passed."""
    allowed = _allowed()
    dropped = next(entry for entry in allowed if entry.startswith("dry-run "))

    report = check_conformance(
        spec_app,
        HIERARCHY,
        allow=[entry for entry in allowed if entry != dropped],
        exempt=protocol_paths(spec_app.routes),
    )

    assert [violation.key for violation in report.violations] == [dropped]


def test_the_protocol_routers_are_exempt_by_name(spec_app):
    exempt = protocol_paths(spec_app.routes)

    assert {"/api/v1/auth/login", "/api/v1/auth/oidc/{provider}/callback"} <= set(exempt)
    assert "/api/v1/scim/v2/Users/{user_id}" in exempt
    assert not [path for path in exempt if path.startswith("/api/v1/auth/accounts")]


@pytest.mark.parametrize(
    ("method", "path", "operation_id", "action"),
    [
        ("get", "/api/v1/sources", "sources.list", "source:read"),
        ("put", "/api/v1/sources/{name}", "sources.apply", "source:write"),
        ("post", "/api/v1/sources/{name}/deploy", "sources.deploy", "source:write"),
        ("get", "/api/v1/auth/accounts", "auth.accounts.list", "account:read"),
        ("put", "/api/v1/system/retention", "system.retention.apply", "system:write"),
    ],
)
def test_each_operation_is_named_by_its_path_and_carries_its_action(
    spec_app, method, path, operation_id, action
):
    operation = spec_app.openapi()["paths"][path][method]

    assert operation["operationId"] == operation_id
    assert operation[EXTENSION]["action"] == action


def test_a_route_checked_in_its_handler_names_no_action(spec_app):
    """account_groups checks its action against the group's own scope, inside the handler."""
    operation = spec_app.openapi()["paths"]["/api/v1/auth/groups/{name}"]["put"]

    assert "action" not in operation[EXTENSION]
