#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_e2e_server.py
#  Purpose:      e2e-server-only API group (seed-admin) is absent outside that mode
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

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
    HuntsSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)

_SEED = "/api/e2e/seed-static"
_STATUS = "/api/e2e/status"


@pytest.fixture(autouse=True)
def _hermetic_break_glass(monkeypatch):
    """Do not inherit DFE_AUTH_LOCAL_ADMIN_* from a developer .env."""
    monkeypatch.delenv("DFE_AUTH_LOCAL_ADMIN_NAME", raising=False)
    monkeypatch.delenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", raising=False)


def _settings(tmp_path: Path, *, e2e_server: bool, env: str = "test") -> DFESettings:
    for sub in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets"):
        (tmp_path / sub).mkdir()
    return DFESettings(
        env=env,
        e2e_server=e2e_server,
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


def test_e2e_routes_absent_when_flag_off(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=False))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get(_STATUS).status_code == 404
            assert client.post(_SEED, json={"script": "seed_admin"}).status_code == 404
    finally:
        _registries.clear()


def test_e2e_openapi_absent_when_flag_off(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=False))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            spec = client.get("/openapi.json").json()
            assert _SEED not in spec["paths"]
            assert _STATUS not in spec["paths"]
            assert "E2E" not in {t["name"] for t in spec.get("tags", [])}
            assert client.get("/openapi.e2e.json").status_code == 404
            docs = client.get("/docs").text
            assert "openapi.e2e.json" not in docs
    finally:
        _registries.clear()


def test_build_e2e_spec_documents_helpers_only():
    from dfe_engine.api.e2e_docs import build_e2e_spec

    spec = build_e2e_spec(version="dev")
    assert set(spec["paths"]) == {_STATUS, _SEED}
    assert spec["paths"][_STATUS]["get"]["tags"] == ["E2E"]
    assert spec["paths"][_SEED]["post"]["tags"] == ["E2E"]
    assert spec["paths"][_SEED]["post"].get("security") == []
    e2e_tags = [t for t in spec.get("tags", []) if t["name"] == "E2E"]
    assert len(e2e_tags) == 1
    assert "Playwright" in e2e_tags[0]["description"]
    assert "/api/v1/auth/login" not in spec["paths"]


def test_e2e_openapi_group_when_flag_on(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=True))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            api_spec = client.get("/openapi.json").json()
            e2e_spec = client.get("/openapi.e2e.json").json()
            docs = client.get("/docs").text

        assert _STATUS not in api_spec["paths"]
        assert _SEED not in api_spec["paths"]
        assert "E2E" not in {t["name"] for t in api_spec.get("tags", [])}

        assert _STATUS in e2e_spec["paths"]
        assert _SEED in e2e_spec["paths"]
        assert e2e_spec["paths"][_STATUS]["get"]["tags"] == ["E2E"]
        assert e2e_spec["paths"][_SEED]["post"]["tags"] == ["E2E"]
        assert e2e_spec["paths"][_SEED]["post"].get("security") == []
        e2e_tags = [t for t in e2e_spec.get("tags", []) if t["name"] == "E2E"]
        assert len(e2e_tags) == 1
        assert "Playwright" in e2e_tags[0]["description"]
        assert "/api/v1/auth/login" not in e2e_spec["paths"]

        assert "openapi.e2e.json" in docs
        assert "StandaloneLayout" in docs
        assert '"name": "API"' in docs or '"name":"API"' in docs
        assert '"name": "E2E"' in docs or '"name":"E2E"' in docs
    finally:
        _registries.clear()


def test_e2e_status_and_seed_admin_when_flag_on(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=True))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            status = client.get(_STATUS)
            assert status.status_code == 200
            assert status.json() == {"enabled": True}

            # Drop the bootstrap admin so seed_admin has to create one.
            app.state.account_store.delete("admin")
            resp = client.post(_SEED, json={"script": "seed_admin"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert "password" not in body

            login = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "changeme"},
            )
            assert login.status_code == 200
            assert "admin" in login.json()["roles"]
    finally:
        _registries.clear()


def test_e2e_seed_admin_resets_existing_password(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=True))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.account_store.reset_password("admin", "first-pass")
            first = client.post(_SEED, json={"script": "seed_admin"})
            assert first.status_code == 200
            assert first.json()["success"] is True

            second = client.post(_SEED, json={"script": "seed_admin"})
            assert second.status_code == 200
            assert second.json()["success"] is True

            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "first-pass"},
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "changeme"},
                ).status_code
                == 200
            )
    finally:
        _registries.clear()
