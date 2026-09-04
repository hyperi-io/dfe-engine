#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_auth_setup_status.py
#  Purpose:      Initial setup status endpoint
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.auth.bootstrap import admin_account_name
from dfe_engine.auth.oidc.models import OIDCProvider
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


def _settings_auth_disabled(tmp_path: Path) -> DFESettings:
    """The local dev posture: DFE_AUTH_ENABLED=false, local auth left unset."""
    settings = _settings(tmp_path)
    return settings.model_copy(update={"auth": settings.auth.model_copy(update={"enabled": False})})


def _complete_setup(app) -> None:
    """Satisfy every required step: org, real user, rotated break-glass password."""
    app.state.org_registry.create("acme", display_name="Acme")
    app.state.account_store.create("alice", "a-strong-user-password", groups=["dfe-admins"])
    app.state.account_store.reset_password(admin_account_name(), "a-strong-local-admin-password")


def test_setup_status_public_and_incomplete_on_fresh_bootstrap(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/auth/setup-status")
            assert resp.status_code == 200
            setup = resp.json()["initial_setup"]
            assert setup["complete"] is False
            # Optional OIDC is listed in steps but never in pending_steps.
            assert setup["steps"] == [
                "oidc_provider",
                "organisations",
                "first_user",
                "admin_password",
            ]
            assert setup["pending_steps"] == ["organisations", "first_user", "admin_password"]
            assert setup["completed_steps"] == []
            assert setup["current_step"] == "organisations"
    finally:
        _registries.clear()


def test_setup_status_measures_rotation_against_the_configured_password(tmp_path):
    """A deployment that generated its own bootstrap password has still not rotated it."""
    settings = _settings(tmp_path)
    local = LocalAuthSettings(enabled=True, admin_password="a-generated-boot-password")
    settings = settings.model_copy(
        update={"auth": settings.auth.model_copy(update={"local": local})}
    )
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            setup = client.get("/api/v1/auth/setup-status").json()["initial_setup"]
            assert "admin_password" in setup["pending_steps"]

            app.state.account_store.reset_password(
                admin_account_name(), "a-strong-local-admin-password"
            )
            setup = client.get("/api/v1/auth/setup-status").json()["initial_setup"]
            assert "admin_password" in setup["completed_steps"]
    finally:
        _registries.clear()


def test_setup_status_reports_auth_steps_even_when_auth_is_disabled(tmp_path):
    """DFE_AUTH_ENABLED=false does not stop bootstrap seeding a live admin/changeme.

    ``bootstrap_auth`` runs unconditionally and ``POST /auth/login`` never
    checks the toggle, so the wizard must still call for the rotation and the
    first real user.
    """
    app = create_app(settings=_settings_auth_disabled(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            setup = client.get("/api/v1/auth/setup-status").json()["initial_setup"]

            assert setup["steps"] == [
                "oidc_provider",
                "organisations",
                "first_user",
                "admin_password",
            ]
            assert setup["pending_steps"] == ["organisations", "first_user", "admin_password"]
            assert setup["complete"] is False
    finally:
        _registries.clear()


def test_setup_status_step_details_carry_required_and_complete(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.org_registry.create("acme", display_name="Acme")

            setup = client.get("/api/v1/auth/setup-status").json()["initial_setup"]
            details = {s["id"]: s for s in setup["step_details"]}

            assert details["oidc_provider"]["required"] is False
            assert details["organisations"]["required"] is True
            assert details["organisations"]["complete"] is True
            assert details["first_user"]["complete"] is False
            # Every step carries copy the wizard can render.
            assert all(s["title"] and s["description"] for s in setup["step_details"])
    finally:
        _registries.clear()


def test_setup_status_complete_when_all_required_steps_done(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            _complete_setup(app)

            body = client.get("/api/v1/auth/setup-status").json()
            setup = body["initial_setup"]
            assert setup["complete"] is True
            assert setup["pending_steps"] == []
            assert setup["current_step"] is None
            assert setup["completed_steps"] == ["organisations", "first_user", "admin_password"]
    finally:
        _registries.clear()


def test_setup_status_partial_completion(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.org_registry.create("acme", display_name="Acme")

            setup = client.get("/api/v1/auth/setup-status").json()["initial_setup"]
            assert setup["complete"] is False
            assert setup["pending_steps"] == ["first_user", "admin_password"]
            assert setup["completed_steps"] == ["organisations"]
            assert setup["current_step"] == "first_user"
    finally:
        _registries.clear()


def test_setup_status_returns_registries_while_incomplete(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.org_registry.create("acme", display_name="Acme")
            app.state.account_store.create("alice", "a-strong-user-password")

            body = client.get("/api/v1/auth/setup-status").json()

            assert [o["name"] for o in body["organisations"]] == ["acme"]
            assert body["oidc_providers"] == []
    finally:
        _registries.clear()


def test_setup_status_never_returns_accounts(tmp_path):
    """The account list is not served pre-login — only the first_user verdict is."""
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.account_store.create("alice", "a-strong-user-password")

            body = client.get("/api/v1/auth/setup-status").json()

            assert "accounts" not in body
            assert "alice" not in json.dumps(body)
            assert "first_user" in body["initial_setup"]["completed_steps"]
    finally:
        _registries.clear()


def test_setup_status_withholds_registries_once_complete(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            _complete_setup(app)

            body = client.get("/api/v1/auth/setup-status").json()
            assert body["initial_setup"]["complete"] is True
            assert body["organisations"] == []
            assert body["oidc_providers"] == []
    finally:
        _registries.clear()


def test_setup_status_keeps_oidc_login_options_once_complete(tmp_path):
    """Post-setup the login screen still gets the enabled IdPs — name + label only."""
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.oidc_provider_registry.create(
                "entra",
                OIDCProvider(
                    enabled=True,
                    display_name="Microsoft Entra",
                    issuer="https://idp",
                    client_secret_env="ENTRA_SECRET",
                ),
            )
            app.state.oidc_provider_registry.create(
                "okta",
                OIDCProvider(enabled=False, issuer="https://okta"),
            )
            _complete_setup(app)

            body = client.get("/api/v1/auth/setup-status").json()

            assert body["initial_setup"]["complete"] is True
            assert body["oidc_providers"] == [
                {"name": "entra", "display_name": "Microsoft Entra"},
            ]
            assert "https://idp" not in json.dumps(body)
    finally:
        _registries.clear()
