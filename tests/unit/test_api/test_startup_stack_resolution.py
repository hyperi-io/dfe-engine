#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_startup_stack_resolution.py
#  Purpose:      The stack resolution never holds the lifespan, and a deploy-repo pin skips it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Startup resolves the running stack in the background, never before it serves.

The registry is a real loopback HTTP server. A registry that stalls must not hold the
lifespan, shutdown must cancel the resolution cleanly, and a stack the deploy repo pins
is never looked up because the pin wins.
"""

import secrets
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    GitopsPackSettings,
    GitopsSettings,
    HuntsSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)
from tests.support.oci_registry import ALL_V1, FakeRegistry, running, serve_registry

STALL_SECONDS = 3.0
PINS = 'base:\n  dfe-infra: "2.2.0-rc.13"\n'


@pytest.fixture
def registry():
    with serve_registry() as fake:
        yield fake


def _settings(tmp_path: Path, registry: FakeRegistry) -> DFESettings:
    """A deployment with a deploy repo that runs ALL_V1's images."""
    for sub in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets", "orgs"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return DFESettings(
        env="dev",
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        schemas=SchemasSettings(schemas_dir=str(tmp_path / "schemas")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            local=LocalAuthSettings(enabled=True, admin_password=secrets.token_urlsafe(16)),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
        gitops=GitopsSettings(
            enabled=True,
            local_path=str(tmp_path / "deploy"),
            push=False,
            mode="solo",
            pack=GitopsPackSettings(interval_seconds=3600.0),
        ),
        stack_images=running(ALL_V1),
        stack_manifest_repository=registry.repository,
        stack_version="2.2.2-rc.5",
    )


def _await(task, what: str) -> None:
    deadline = time.monotonic() + 30
    while not task.done():
        assert time.monotonic() < deadline, what
        time.sleep(0.1)


def test_a_stalled_registry_never_holds_startup_and_shutdown_cancels_the_resolution(
    tmp_path: Path, registry: FakeRegistry
) -> None:
    registry.stacks = {"2.2.2": ALL_V1}
    registry.delay_seconds = STALL_SECONDS
    app = create_app(settings=_settings(tmp_path, registry))
    try:
        began = time.monotonic()
        with TestClient(app, raise_server_exceptions=False) as client:
            assert time.monotonic() - began < STALL_SECONDS
            assert client.get("/livez").status_code == 200
            resolution = app.state.stack_resolution
            assert not resolution.done()
            assert app.state.resolved_stack is None
        assert resolution.cancelled()
    finally:
        _registries.clear()


def test_the_registry_answers_once_startup_is_done(tmp_path: Path, registry: FakeRegistry) -> None:
    registry.stacks = {"2.2.2": ALL_V1}
    app = create_app(settings=_settings(tmp_path, registry))
    try:
        with TestClient(app, raise_server_exceptions=False):
            _await(app.state.stack_resolution, "the stack resolution never finished")
            assert app.state.resolved_stack == "2.2.2"
    finally:
        _registries.clear()


def test_a_stack_the_deploy_repo_pins_is_never_looked_up(
    tmp_path: Path, registry: FakeRegistry
) -> None:
    registry.stacks = {"2.2.2": ALL_V1}
    settings = _settings(tmp_path, registry)
    # A first start creates the deploy repo, and the pin lands in its working tree as a publish leaves it.
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False):
            _await(app.state.stack_resolution, "the first resolution never finished")
        (tmp_path / "deploy" / "pins.yaml").write_text(PINS)
        registry.requests = 0
        app = create_app(settings=settings)
        with TestClient(app, raise_server_exceptions=False):
            _await(app.state.stack_resolution, "the pinned resolution never finished")
            assert app.state.resolved_stack is None
        assert registry.requests == 0
    finally:
        _registries.clear()
