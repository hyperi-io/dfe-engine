#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_auth_setup_status.py
#  Purpose:      Initial setup status endpoint
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import json
import secrets
from pathlib import Path

from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.auth import breakglass
from dfe_engine.auth.bootstrap import admin_account_name, admin_account_password
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    GitopsSettings,
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


MINTED_ADMIN = "a-minted-admin-password"
MINTED_BREAKGLASS = "a-minted-breakglass-password"
# The password an unset config issues to the admin.
SHIPPED_DEFAULT = admin_account_password()


def _with_deploy_repo(settings: DFESettings, tmp_path: Path) -> DFESettings:
    """Turn on gitops against a local-only deploy repo, so a break-glass hash has a home."""
    settings.gitops = GitopsSettings(enabled=True, local_path=str(tmp_path / "deploy"), push=False)
    return settings


def _deploy_repo_crud(tmp_path: Path) -> GitCrud:
    return GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())


def _complete_setup(app) -> None:
    """Satisfy every required step -- an organisation and a real user.

    The admin password is minted here too, as a deployed engine's is, but it
    gates ``default_credentials`` rather than any wizard step.
    """
    app.state.org_registry.create("acme", display_name="Acme")
    app.state.account_store.create("alice", "a-strong-user-password", groups=["dfe-admins"])
    # The admin password is injected config, so moving off the default is a config change.
    app.state.settings.auth.local.admin_password = MINTED_ADMIN


def test_setup_status_public_and_incomplete_on_fresh_bootstrap(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/v1/auth/setup-status")
            assert resp.status_code == 200
            setup = resp.json()["initial_setup"]
            assert setup["complete"] is False
            # Optional OIDC is listed in steps but never in pending_steps.
            assert setup["steps"] == ["oidc_provider", "organisations", "first_user"]
            assert setup["pending_steps"] == ["organisations", "first_user"]
            assert setup["completed_steps"] == []
            assert setup["current_step"] == "organisations"
    finally:
        _registries.clear()


def _change_admin_password(client: TestClient, new_password: str) -> None:
    """The forced change a console makes: log in on the issued default, then replace it."""
    issued = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": SHIPPED_DEFAULT}
    )
    assert issued.status_code == 200, issued.text
    assert issued.json()["password_change_required"] is True
    changed = client.post(
        "/api/v1/auth/accounts/reset-password",
        json={"current_password": SHIPPED_DEFAULT, "new_password": new_password},
        headers={"Authorization": f"Bearer {issued.json()['access_token']}"},
    )
    assert changed.status_code == 200, changed.text


def test_the_forced_change_clears_default_credentials_without_a_restart(tmp_path):
    """Login, refresh and setup-status all answer from the admin's current password."""
    owner_password = secrets.token_urlsafe(16)
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            body = client.get("/api/v1/auth/setup-status").json()
            assert body["default_credentials"] is True
            assert "admin_password" not in body["initial_setup"]["steps"]

            _change_admin_password(client, owner_password)

            token = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": owner_password}
            )
            assert token.status_code == 200, token.text
            assert token.json()["default_credentials"] is False
            refreshed = client.post(
                "/api/v1/auth/refresh",
                headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            )
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["default_credentials"] is False
            assert client.get("/api/v1/auth/setup-status").json()["default_credentials"] is False
    finally:
        _registries.clear()


def test_a_restart_keeps_the_owners_password_and_the_cleared_flag(tmp_path):
    """Config still carries the default, and the reconcile does not issue it again."""
    settings = _settings(tmp_path)
    owner_password = secrets.token_urlsafe(16)
    try:
        with TestClient(create_app(settings=settings), raise_server_exceptions=False) as client:
            _change_admin_password(client, owner_password)
        _registries.clear()

        with TestClient(create_app(settings=settings), raise_server_exceptions=False) as client:
            stale = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": SHIPPED_DEFAULT}
            )
            assert stale.status_code == 401, stale.text
            token = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": owner_password}
            )
            assert token.status_code == 200, token.text
            assert token.json()["default_credentials"] is False
            assert client.get("/api/v1/auth/setup-status").json()["default_credentials"] is False
    finally:
        _registries.clear()


def test_an_admin_issued_the_default_again_is_flagged_again(tmp_path):
    """A store write that issues the default puts the flag back on the next read."""
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            _change_admin_password(client, secrets.token_urlsafe(16))
            app.state.account_store.reset_password(
                admin_account_name(), SHIPPED_DEFAULT, change_required=True
            )

            assert client.get("/api/v1/auth/setup-status").json()["default_credentials"] is True
    finally:
        _registries.clear()


def test_setup_status_carries_the_deploy_kind_and_fetch_command(tmp_path):
    """The pre-login page shows where to read the password this deployment minted."""
    settings = _settings(tmp_path)
    settings.deployment.target = "kubernetes"
    settings.deployment.namespace = "dfe"
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            body = client.get("/api/v1/auth/setup-status").json()

            assert body["deploy_kind"] == "kubernetes"
            assert body["credential_fetch_command"] == (
                "kubectl -n dfe get secret dfe-engine "
                "-o jsonpath='{.data.admin-password}' | base64 -d"
            )
            assert body["default_credentials"] is True
    finally:
        _registries.clear()


def test_default_credentials_clears_once_the_password_is_minted(tmp_path):
    settings = _settings(tmp_path)
    settings.auth.local.admin_password = MINTED_ADMIN
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/api/v1/auth/setup-status").json()["default_credentials"] is False

            token = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": MINTED_ADMIN}
            )
            assert token.status_code == 200, token.text
            assert token.json()["default_credentials"] is False
    finally:
        _registries.clear()


def test_login_on_the_default_password_flags_the_session(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            token = client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "changeme"}
            )

            assert token.status_code == 200, token.text
            assert token.json()["default_credentials"] is True
    finally:
        _registries.clear()


def test_setup_status_reports_auth_steps_even_when_auth_is_disabled(tmp_path):
    """DFE_AUTH_ENABLED=false does not stop bootstrap seeding a live admin/changeme.

    ``bootstrap_auth`` runs unconditionally and ``POST /auth/login`` never
    checks the toggle, so the wizard must still call for the first real user.
    """
    app = create_app(settings=_settings_auth_disabled(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            setup = client.get("/api/v1/auth/setup-status").json()["initial_setup"]

            assert setup["steps"] == ["oidc_provider", "organisations", "first_user"]
            assert setup["pending_steps"] == ["organisations", "first_user"]
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
            assert setup["completed_steps"] == ["organisations", "first_user"]
    finally:
        _registries.clear()


def test_setup_status_partial_completion(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.org_registry.create("acme", display_name="Acme")

            setup = client.get("/api/v1/auth/setup-status").json()["initial_setup"]
            assert setup["complete"] is False
            assert setup["pending_steps"] == ["first_user"]
            assert setup["completed_steps"] == ["organisations"]
            assert setup["current_step"] == "first_user"
    finally:
        _registries.clear()


def test_setup_status_returns_registries_while_incomplete(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # No real user yet, so the wizard still has a step to render these for.
            app.state.org_registry.create("acme", display_name="Acme")

            body = client.get("/api/v1/auth/setup-status").json()

            assert body["initial_setup"]["complete"] is False
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


def test_break_glass_logs_in_from_the_committed_hash(tmp_path):
    """The hash in governance settings is what the recovery login verifies against."""
    settings = _with_deploy_repo(_settings(tmp_path), tmp_path)
    settings.auth.local.breakglass_password = MINTED_BREAKGLASS
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": breakglass.USERNAME, "password": MINTED_BREAKGLASS},
            )

            assert resp.status_code == 200, resp.text
            assert resp.json()["roles"] == ["admin"]
    finally:
        _registries.clear()


def test_disabled_break_glass_login_is_403_with_a_reason(tmp_path):
    settings = _with_deploy_repo(_settings(tmp_path), tmp_path)
    settings.auth.local.breakglass_password = MINTED_BREAKGLASS
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            breakglass.set_enabled(_deploy_repo_crud(tmp_path), False, "tester")

            resp = client.post(
                "/api/v1/auth/login",
                json={"username": breakglass.USERNAME, "password": MINTED_BREAKGLASS},
            )

            assert resp.status_code == 403
            body = resp.json()
            assert body["code"] == "breakglass_disabled"
            assert breakglass.KEY_ENABLED in body["message"]
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
