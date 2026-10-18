#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_recovery_account_passwords.py
#  Purpose:      Changing the admin and break-glass passwords on every deployment shape
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The console's password action on the two recovery accounts, end to end over the API.

Each shape is a booted app over a real local deploy repo and the file secrets
backend: gitops off, a direct-commit deployment, and a review deployment with and
without a forge to open the pull request. A change only counts when it is still
the password after a restart, so most tests boot the engine a second time.

Every password is generated at import: a literal here would be a credential to
whoever copies the fixture next.
"""

import contextlib
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.auth import breakglass
from dfe_engine.auth.accounts import matches_digest
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DeploymentSettings,
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
MINTED_BREAKGLASS = secrets.token_urlsafe(16)
MINTED_USER = secrets.token_urlsafe(16)

DEV = "dev"
PROD = "production"
_ACCOUNTS = "/api/v1/auth/accounts"
_PR_URL = "https://forge.example/deploy/pulls/9"

# Words a person reading the console would not use for what happened.
_JARGON = ("seam", "store operation", "store-backed", "posture", "gitcrud")


def _settings(
    tmp_path: Path,
    *,
    gitops: bool,
    env: str = DEV,
    mode: Literal["solo", "team"] = "team",
    auth_sub: str = "auth",
    secret_path: str = "",
    deploy_kind: Literal["kubernetes", "docker", "unknown"] = "kubernetes",
) -> DFESettings:
    """One deployment shape. ``auth_sub`` names the live account store's directory."""
    for sub in ("sources", "services", "rules", "hunts", auth_sub, "schemas", "secrets"):
        (tmp_path / sub).mkdir(exist_ok=True)
    settings = DFESettings(
        env=env,
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        deployment=DeploymentSettings(target=deploy_kind, namespace="dfe"),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        schemas=SchemasSettings(schemas_dir=str(tmp_path / "schemas")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / auth_sub),
            local=LocalAuthSettings(
                enabled=True,
                admin_password=MINTED_ADMIN,
                breakglass_password=MINTED_BREAKGLASS,
                admin_password_secret_path=secret_path,
            ),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )
    if gitops:
        settings.gitops = GitopsSettings(
            enabled=True, local_path=str(tmp_path / "deploy"), push=False, mode=mode
        )
    return settings


@contextlib.contextmanager
def _booted(settings: DFESettings) -> Iterator[tuple[FastAPI, TestClient]]:
    """A started engine; leaving the block stops it, as a pod being replaced does."""
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield app, client
    finally:
        _registries.clear()


def _headers(settings: DFESettings, username: str) -> dict[str, str]:
    """Headers for *username*; roles resolve live from group membership."""
    token = create_access_token(
        data={"sub": username, "org_id": "acme", "roles": []}, settings=settings
    )
    return {"Authorization": f"Bearer {token}"}


def _alice(app: FastAPI, settings: DFESettings) -> dict[str, str]:
    """An admin of the deployment's own, who changes the recovery accounts' passwords."""
    store = app.state.account_store
    if store.get("alice") is None:
        store.create("alice", MINTED_USER, groups=["dfe-admins"])
        app.state.group_store.add_member("dfe-admins", "alice")
    return _headers(settings, "alice")


def _reset(client: TestClient, headers: dict[str, str], username: str, new: str) -> dict:
    resp = client.post(
        f"{_ACCOUNTS}/{username}/reset-password", json={"new_password": new}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _logs_in(client: TestClient, username: str, password: str) -> bool:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return resp.status_code == 200


def _assert_plain(message: str) -> None:
    lowered = message.lower()
    for word in _JARGON:
        assert word not in lowered, f"{word!r} in {message!r}"


class _RecordingForge:
    """Opens a pull request and records it; the forge's HTTP call is not under test."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        self.opened.append(head)
        return PullRequest(number=9, url=_PR_URL, branch=head)


# ── breakglass: its hash lives in the deploy repo ────────────


class TestBreakglassOnADirectCommitDeployment:
    """Dev, solo or auto-merge: the new hash lands on the deploy repo's main at once."""

    def test_the_new_password_is_committed_and_reported_durable(self, tmp_path):
        settings = _settings(tmp_path, gitops=True)
        new = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            body = _reset(client, _alice(app, settings), "breakglass", new)

            git = body["git"]
            assert (git["enabled"], git["committed"], git["merged"]) == (True, True, True)
            assert git["pending"] is None
            assert matches_digest(new, breakglass.stored_hash(app.state.gitcrud))
            assert _logs_in(client, "breakglass", new)

    def test_a_rebuilt_engine_keeps_the_new_password(self, tmp_path):
        settings = _settings(tmp_path, gitops=True)
        new = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            _reset(client, _alice(app, settings), "breakglass", new)

        rebuilt = _settings(tmp_path, gitops=True, auth_sub="rebuilt-auth")
        with _booted(rebuilt) as (app, client):
            assert _logs_in(client, "breakglass", new)
            assert not _logs_in(client, "breakglass", MINTED_BREAKGLASS)

    def test_a_restart_on_the_same_store_keeps_the_new_password(self, tmp_path):
        settings = _settings(tmp_path, gitops=True)
        new = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            _reset(client, _alice(app, settings), "breakglass", new)

        with _booted(settings) as (app, client):
            assert app.state.account_store.verify_password("breakglass", new)

    def test_git_status_reports_it_merged(self, tmp_path):
        settings = _settings(tmp_path, gitops=True)
        with _booted(settings) as (app, client):
            headers = _alice(app, settings)
            _reset(client, headers, "breakglass", secrets.token_urlsafe(16))

            resp = client.get(f"{_ACCOUNTS}/breakglass/git-status", headers=headers)

            assert resp.status_code == 200, resp.text
            assert (resp.json()["enabled"], resp.json()["merged"]) == (True, True)

    def test_breakglass_changing_its_own_password_keeps_it(self, tmp_path):
        settings = _settings(tmp_path, gitops=True)
        new = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            resp = client.post(
                f"{_ACCOUNTS}/reset-password",
                json={"current_password": MINTED_BREAKGLASS, "new_password": new},
                headers=_headers(settings, "breakglass"),
            )
            assert resp.status_code == 200, resp.text

        rebuilt = _settings(tmp_path, gitops=True, auth_sub="rebuilt-auth")
        with _booted(rebuilt) as (app, client):
            assert _logs_in(client, "breakglass", new)


class TestBreakglassOnAReviewDeployment:
    """Production with DFE_GITOPS_MODE=team: main changes only when the review merges."""

    def test_with_a_forge_the_change_waits_on_the_pull_request(self, tmp_path):
        settings = _settings(tmp_path, gitops=True, env=PROD)
        new = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            forge = _RecordingForge()
            app.state.forge = forge

            git = _reset(client, _alice(app, settings), "breakglass", new)["git"]

            assert (git["enabled"], git["committed"], git["merged"]) == (True, True, False)
            assert git["pending"]["pr_url"] == _PR_URL
            assert len(forge.opened) == 1
            # main still carries the old hash until someone merges the review.
            assert matches_digest(MINTED_BREAKGLASS, breakglass.stored_hash(app.state.gitcrud))
            assert _logs_in(client, "breakglass", new)

    def test_without_a_forge_it_hands_back_the_merge_command(self, tmp_path):
        settings = _settings(tmp_path, gitops=True, env=PROD)
        with _booted(settings) as (app, client):
            app.state.forge = None
            headers = _alice(app, settings)

            git = _reset(client, headers, "breakglass", secrets.token_urlsafe(16))["git"]

            assert (git["committed"], git["merged"]) == (True, False)
            assert git["pending"]["pr_url"] is None
            assert git["pending"]["branch"]
            assert "git merge" in git["pending"]["command"]
            assert git["pending"]["branch"] in git["pending"]["command"]
            status = client.get(f"{_ACCOUNTS}/breakglass/git-status", headers=headers).json()
            assert (status["enabled"], status["merged"]) == (True, False)


# ── admin: its first password comes from the deployment ──────


class TestAdminReset:
    @pytest.mark.parametrize("gitops", [True, False], ids=["gitops", "no-gitops"])
    def test_a_reset_survives_a_restart(self, tmp_path, gitops):
        settings = _settings(tmp_path, gitops=gitops)
        new = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            body = _reset(client, _alice(app, settings), "admin", new)
            assert body["git"]["enabled"] is gitops
            assert body["git"]["merged"] is True

        with _booted(settings) as (app, client):
            assert _logs_in(client, "admin", new)
            assert not _logs_in(client, "admin", MINTED_ADMIN)

    def test_a_rebuilt_engine_restores_the_reset_from_the_deploy_repo(self, tmp_path):
        settings = _settings(tmp_path, gitops=True)
        new = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            _reset(client, _alice(app, settings), "admin", new)

        rebuilt = _settings(tmp_path, gitops=True, auth_sub="rebuilt-auth")
        with _booted(rebuilt) as (app, client):
            assert _logs_in(client, "admin", new)


class TestRotate:
    """Rotate writes the value the deployment injects; only the admin has one."""

    def test_breakglass_is_pointed_at_reset_in_plain_words(self, tmp_path):
        settings = _settings(tmp_path, gitops=True)
        with _booted(settings) as (app, client):
            resp = client.post(
                f"{_ACCOUNTS}/breakglass/rotate-password",
                json={"new_password": secrets.token_urlsafe(16)},
                headers=_alice(app, settings),
            )

        assert resp.status_code == 400
        assert resp.json()["code"] == "not_store_backed"
        message = resp.json()["message"]
        _assert_plain(message)
        assert "breakglass" in message
        assert "reset" in message.lower()

    @pytest.mark.parametrize(
        ("kind", "command"),
        [
            ("kubernetes", "kubectl -n dfe patch secret dfe-engine"),
            ("docker", "make up"),
        ],
    )
    def test_without_a_secret_path_it_names_where_to_change_it(self, tmp_path, kind, command):
        settings = _settings(tmp_path, gitops=False, deploy_kind=kind)
        with _booted(settings) as (app, client):
            resp = client.post(
                f"{_ACCOUNTS}/admin/rotate-password",
                json={"new_password": secrets.token_urlsafe(16)},
                headers=_alice(app, settings),
            )

        assert resp.status_code == 501
        body = resp.json()
        assert body["code"] == "admin_password_kept_outside_dfe"
        _assert_plain(body["message"])
        assert command in body["message"]
        assert command in body["context"]["store_command"]

    def test_with_a_secret_path_it_writes_the_secret_store(self, tmp_path):
        settings = _settings(tmp_path, gitops=False, secret_path="auth/admin-password")
        new = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            resp = client.post(
                f"{_ACCOUNTS}/admin/rotate-password",
                json={"new_password": new},
                headers=_alice(app, settings),
            )

        assert resp.status_code == 200, resp.text
        _assert_plain(resp.json()["message"])
        assert build_secrets(settings.secrets).get("auth/admin-password") == new


# ── The admin is kept, on every shape ────────────────────────


def _complete_setup(app: FastAPI, settings: DFESettings) -> dict[str, str]:
    """An organisation and an admin of the deployment's own: setup is complete."""
    app.state.org_registry.create("acme", display_name="Acme")
    return _alice(app, settings)


class TestTheAdminIsKept:
    @pytest.mark.parametrize("gitops", [True, False], ids=["gitops", "no-gitops"])
    def test_completing_setup_keeps_the_admin_enabled_and_listed(self, tmp_path, gitops):
        settings = _settings(tmp_path, gitops=gitops)
        with _booted(settings) as (app, client):
            headers = _complete_setup(app, settings)

            status = client.get("/api/v1/auth/setup-status").json()
            listed = client.get(f"{_ACCOUNTS}?include_core=true", headers=headers).json()

            assert status["initial_setup"]["complete"] is True
            assert status["admin_retired"] is False
            names = {row["username"]: row for row in listed["items"]}
            assert names["admin"]["enabled"] is True
            assert ("breakglass" in names) is gitops

    @pytest.mark.parametrize("gitops", [True, False], ids=["gitops", "no-gitops"])
    def test_after_setup_the_admin_password_can_be_reset(self, tmp_path, gitops):
        settings = _settings(tmp_path, gitops=gitops)
        new = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            headers = _complete_setup(app, settings)

            _reset(client, headers, "admin", new)

            assert _logs_in(client, "admin", new)


class TestTheE2eResetKeepsBothRecoveryAccounts:
    def test_reset_all_puts_breakglass_back_on_its_configured_password(self, tmp_path):
        settings = _settings(tmp_path, gitops=True, env="test")
        settings.e2e_server = True
        changed = secrets.token_urlsafe(16)
        with _booted(settings) as (app, client):
            _reset(client, _alice(app, settings), "breakglass", changed)

            resp = client.post("/api/e2e/seed-static", json={"script": "reset_all"})
            assert resp.status_code == 200, resp.text

            assert _logs_in(client, "breakglass", MINTED_BREAKGLASS)
            assert not _logs_in(client, "breakglass", changed)
            assert "breakglass" in app.state.group_store.get("dfe-admins").members
            assert matches_digest(MINTED_BREAKGLASS, breakglass.stored_hash(app.state.gitcrud))
