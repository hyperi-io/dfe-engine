#  Project:      dfe-engine
#  File:         tests/unit/test_exchange/conftest.py
#  Purpose:      Stand up a whole deployment per exchange test
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Build a deployment over its own directory tree.

Import and export only mean anything between two deployments, and the registries
are a process-wide bootstrap, so a test builds one app, finishes with it, and
then builds the next.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)
from dfe_engine.yaml_utils import yaml_dump
from tests.support.core_sources import write_landing_definition

ADMIN_PASSWORD = "test-admin-pw"

CUSTOM_SCHEMA = {
    "current": "1.1.0",
    "resource_type": "custom",
    "versions": {
        "1.0.0": {
            "date": "2026-01-01",
            "type": "model",
            "summary": "init",
            "columns": [{"name": "host_name", "type": "string", "expr": "@source: host.name"}],
        },
        "1.1.0": {
            "date": "2026-02-01",
            "type": "addition",
            "summary": "add the event code",
            "columns": [
                {"name": "host_name", "type": "string", "expr": "@source: host.name"},
                {"name": "event_code", "type": "string", "expr": "@source: event.code"},
            ],
        },
    },
}

CORE_SCHEMA = {
    "current": "2.0.0",
    "resource_type": "core",
    "versions": {
        "2.0.0": {
            "date": "2026-03-01",
            "type": "model",
            "summary": "pre-supplied",
            "columns": [{"name": "observed_at", "type": "datetime64"}],
        }
    },
}


def _settings(root: Path, *, schemas: dict[str, dict]) -> DFESettings:
    """A deployment rooted at *root*, seeded with *schemas* by registry path."""
    for name in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets"):
        (root / name).mkdir(parents=True, exist_ok=True)
    schemas_dir = root / "schemas"
    write_landing_definition(schemas_dir=schemas_dir)
    for path, document in schemas.items():
        yaml_dump(document, schemas_dir / f"{path}.yaml")

    return DFESettings(
        config_dir=str(root),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        source=SourceSettings(sources_dir=str(root / "sources")),
        services=ServicesSettings(config_yaml_dir=str(root / "services")),
        schemas=SchemasSettings(schemas_dir=str(schemas_dir)),
        hunts=HuntsSettings(rules_dir=str(root / "rules"), hunt_dir=str(root / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(root / "auth"),
            trust_proxy_auth_headers=True,
            local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
        ),
        secrets=SecretsSettings(provider="file", path=str(root / "secrets")),
        api=APISettings(
            jwt_secret="test-secret-key-for-unit-tests-hmac32",
            jwt_expire_minutes=30,
        ),
    )


type Deployment = Callable[..., tuple[TestClient, dict[str, str]]]


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    """Build a named deployment: returns its client and the admin headers for it."""

    def build(name: str, *, schemas: dict[str, dict]) -> tuple[TestClient, dict[str, str]]:
        _registries.clear()
        settings = _settings(tmp_path / name, schemas=schemas)
        token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=settings,
        )
        client = TestClient(create_app(settings=settings), raise_server_exceptions=False)
        return client, {"Authorization": f"Bearer {token}"}

    return build


@pytest.fixture(autouse=True)
def _clear_registries():
    """The registries are a process-wide bootstrap; two apps must not share one."""
    _registries.clear()
    yield
    _registries.clear()
