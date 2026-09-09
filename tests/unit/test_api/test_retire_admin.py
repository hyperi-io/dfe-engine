#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_retire_admin.py
#  Purpose:      POST /auth/setup/retire-admin and the setup-status hint it pairs with
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Retiring the bootstrap admin, end to end over the API.

Every password is generated at import: a literal here would be a credential to
whoever copies the fixture next, and the retirement flow exists precisely so the
minted one can be deleted.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.auth import admin_retirement
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

MINTED_ADMIN = secrets.token_urlsafe(16)
MINTED_USER = secrets.token_urlsafe(16)


def _settings(tmp_path: Path, *, gitops: bool = True, auth_sub: str = "auth") -> DFESettings:
    for sub in ("sources", "services", "rules", "hunts", auth_sub, "schemas", "secrets"):
        (tmp_path / sub).mkdir(exist_ok=True)
    settings = DFESettings(
        env="dev",
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        schemas=SchemasSettings(schemas_dir=str(tmp_path / "schemas")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / auth_sub),
            local=LocalAuthSettings(enabled=True, admin_password=MINTED_ADMIN),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )
    if gitops:
        settings.gitops = GitopsSettings(
            enabled=True, local_path=str(tmp_path / "deploy"), push=False
        )
    return settings


def _deploy_repo_crud(tmp_path: Path) -> GitCrud:
    return GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())


def _token(settings: DFESettings, username: str) -> dict[str, str]:
    """Headers for *username*; roles resolve live from group membership."""
    token = create_access_token(
        data={"sub": username, "org_id": "acme", "roles": []}, settings=settings
    )
    return {"Authorization": f"Bearer {token}"}


def _complete_setup(app, *, admin_of_our_own: bool = True) -> None:
    """Satisfy every required step, optionally without an admin of the org's own."""
    app.state.org_registry.create("acme", display_name="Acme")
    groups = ["dfe-admins"] if admin_of_our_own else ["dfe-viewers"]
    app.state.account_store.create("alice", MINTED_USER, groups=groups)
    app.state.group_store.add_member(groups[0], "alice")


@pytest.fixture
def wired(tmp_path: Path):
    """A booted app on a local deploy repo, with setup complete and alice an admin."""
    settings = _settings(tmp_path)
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            _complete_setup(app)
            yield app, client, settings
    finally:
        _registries.clear()


class TestRetireAdmin:
    def test_the_hint_is_offered_before_and_withdrawn_after(self, wired, tmp_path):
        app, client, settings = wired

        before = client.get("/api/v1/auth/setup-status").json()
        assert before["admin_username"] == "admin"
        assert before["admin_retired"] is False
        assert before["retire_admin_available"] is True

        resp = client.post("/api/v1/auth/setup/retire-admin", headers=_token(settings, "alice"))

        assert resp.status_code == 200, resp.text
        assert resp.json()["admin_retired"] is True
        assert resp.json()["retire_admin_available"] is False
        assert client.get("/api/v1/auth/setup-status").json()["admin_retired"] is True

    def test_it_disables_the_account_and_records_the_fact(self, wired, tmp_path):
        app, client, settings = wired

        client.post("/api/v1/auth/setup/retire-admin", headers=_token(settings, "alice"))

        assert app.state.account_store.get("admin").enabled is False
        crud = _deploy_repo_crud(tmp_path)
        assert admin_retirement.is_retired(crud) is True
        # The durable copy is disabled too, so a rebuild cannot hand the credential back.
        assert crud.get("accounts", "admin")["enabled"] is False

    def test_the_retired_admin_can_no_longer_log_in(self, wired, tmp_path):
        app, client, settings = wired

        client.post("/api/v1/auth/setup/retire-admin", headers=_token(settings, "alice"))

        resp = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": MINTED_ADMIN}
        )
        assert resp.status_code == 401

    def test_the_admin_may_retire_itself(self, wired, tmp_path):
        app, client, settings = wired

        resp = client.post("/api/v1/auth/setup/retire-admin", headers=_token(settings, "admin"))

        assert resp.status_code == 200, resp.text
        assert resp.json()["admin_retired"] is True

    def test_a_second_call_is_a_no_op(self, wired, tmp_path):
        app, client, settings = wired
        headers = _token(settings, "alice")
        client.post("/api/v1/auth/setup/retire-admin", headers=headers)

        resp = client.post("/api/v1/auth/setup/retire-admin", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["admin_retired"] is True

    def test_a_non_admin_caller_is_refused(self, wired, tmp_path):
        app, client, settings = wired
        app.state.account_store.create("bob", MINTED_USER, groups=["dfe-viewers"])
        app.state.group_store.add_member("dfe-viewers", "bob")

        resp = client.post("/api/v1/auth/setup/retire-admin", headers=_token(settings, "bob"))

        assert resp.status_code == 403
        assert admin_retirement.is_retired(_deploy_repo_crud(tmp_path)) is False

    def test_refused_without_an_admin_of_the_deployments_own(self, tmp_path):
        settings = _settings(tmp_path)
        app = create_app(settings=settings)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                _complete_setup(app, admin_of_our_own=False)

                status = client.get("/api/v1/auth/setup-status").json()
                assert status["retire_admin_available"] is False

                resp = client.post(
                    "/api/v1/auth/setup/retire-admin", headers=_token(settings, "admin")
                )

                assert resp.status_code == 409
                assert resp.json()["code"] == "retire_admin_unavailable"
                assert app.state.account_store.get("admin").enabled is True
        finally:
            _registries.clear()

    def test_refused_without_a_deploy_repo_to_record_it_in(self, tmp_path):
        """No gitops means nowhere durable for the fact, so the next boot would reseed."""
        settings = _settings(tmp_path, gitops=False)
        app = create_app(settings=settings)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                _complete_setup(app)

                assert (
                    client.get("/api/v1/auth/setup-status").json()["retire_admin_available"]
                    is False
                )
                resp = client.post(
                    "/api/v1/auth/setup/retire-admin", headers=_token(settings, "alice")
                )

                assert resp.status_code == 409
                assert resp.json()["code"] == "retire_admin_unavailable"
        finally:
            _registries.clear()


class TestRetiredDeploymentBoots:
    def test_it_starts_with_the_minted_password_deleted(self, wired, tmp_path):
        """The whole point: a production rebuild with no admin password in the Secret."""
        app, client, settings = wired
        client.post("/api/v1/auth/setup/retire-admin", headers=_token(settings, "alice"))
        _registries.clear()

        # A rebuilt pod: a fresh auth dir, the same deploy repo, no injected password.
        rebuilt = _settings(tmp_path, auth_sub="rebuilt-auth")
        rebuilt.env = "production"
        rebuilt.auth.local.admin_password = ""
        rebuilt_app = create_app(settings=rebuilt)
        try:
            with TestClient(rebuilt_app, raise_server_exceptions=False) as rebuilt_client:
                assert rebuilt_client.get("/api/v1/auth/setup-status").json()["admin_retired"]
                admin = rebuilt_app.state.account_store.get("admin")
                assert admin is None or admin.enabled is False
        finally:
            _registries.clear()
