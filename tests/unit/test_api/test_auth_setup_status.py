#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_auth_setup_status.py
#  Purpose:      Initial setup status endpoint
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
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


def _settings(tmp_path: Path) -> DFESettings:
    for sub in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets", "orgs"):
        (tmp_path / sub).mkdir()
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
            local=LocalAuthSettings(enabled=True, admin_password="changeme"),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )


def test_setup_status_public_and_incomplete_on_fresh_bootstrap(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/auth/setup-status")
            assert resp.status_code == 200
            body = resp.json()
            assert body["initial_setup_required"] is True
            assert body["setup_complete"] is False
            assert body["pending_steps"] == ["organisations", "admin_password"]
            assert body["completed_steps"] == []

    finally:
        _registries.clear()


def test_setup_status_complete_after_password_and_org(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.account_store.reset_password("admin", "a-strong-local-admin-password")
            app.state.org_registry.create("acme", display_name="Acme")

            resp = client.get("/api/v1/auth/setup-status")
            assert resp.status_code == 200
            body = resp.json()
            assert body["initial_setup_required"] is False
            assert body["setup_complete"] is True
            assert body["pending_steps"] == []
            assert body["completed_steps"] == ["organisations", "admin_password"]
    finally:
        _registries.clear()


def test_setup_status_partial_completion(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.org_registry.create("acme", display_name="Acme")

            body = client.get("/api/v1/auth/setup-status").json()
            assert body["initial_setup_required"] is True
            assert body["pending_steps"] == ["admin_password"]
            assert body["completed_steps"] == ["organisations"]
    finally:
        _registries.clear()
