#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sessions_after_restart.py
#  Purpose:      An ended session stays ended when the engine starts again on its deploy repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every way a session ends, then the engine starting again on the same deploy remote.

Each boot restores the accounts the deploy repo carries over the live store, and
reseeds ``breakglass`` from the hash on main, so a session end the deploy repo never
heard of comes undone at the next start. A restarted pod keeps its account store and
clone. A rebuilt one starts on an empty store and a fresh clone of the remote.

Every password is generated at import: a literal here would be a credential to
whoever copies the fixture next.
"""

import contextlib
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from support.deploy_remote import seeded_remote

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
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
MINTED_BREAKGLASS = secrets.token_urlsafe(16)

_LOGIN = "/api/v1/auth/login"
_LOGOUT = "/api/v1/auth/logout"
_ME = "/api/v1/auth/me"
_OWN_CHANGE = "/api/v1/auth/accounts/reset-password"

_FIRST = "first"
_REBUILT = "rebuilt"
_POD_AFTER = pytest.mark.parametrize("pod_after", [_FIRST, _REBUILT], ids=["restarted", "rebuilt"])


class _Deployment:
    """One deployment on one bare deploy remote. Each boot is a pod starting on it."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
        remote, self._branch = seeded_remote(tmp_path)
        self._remote = str(remote)
        for sub in ("sources", "services", "rules", "hunts", "schemas", "secrets"):
            (tmp_path / sub).mkdir(exist_ok=True)

    def _settings(self, pod: str, env: str, mode: Literal["solo", "team"]) -> DFESettings:
        tmp = self._tmp
        (tmp / pod / "auth").mkdir(parents=True, exist_ok=True)
        return DFESettings(
            env=env,
            config_dir=str(tmp),
            clickhouse=ClickHouseSettings(bootstrap_tables=False),
            source=SourceSettings(sources_dir=str(tmp / "sources")),
            services=ServicesSettings(config_yaml_dir=str(tmp / "services")),
            schemas=SchemasSettings(schemas_dir=str(tmp / "schemas")),
            hunts=HuntsSettings(rules_dir=str(tmp / "rules"), hunt_dir=str(tmp / "hunts")),
            auth=AuthSettings(
                enabled=True,
                auth_dir=str(tmp / pod / "auth"),
                local=LocalAuthSettings(
                    enabled=True,
                    admin_password=MINTED_ADMIN,
                    breakglass_password=MINTED_BREAKGLASS,
                ),
            ),
            # The signing key outlives every pod, as a deployment's secrets backend does.
            secrets=SecretsSettings(provider="file", path=str(tmp / "secrets")),
            api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
            gitops=GitopsSettings(
                enabled=True,
                repo_url=self._remote,
                branch=self._branch,
                local_path=str(tmp / pod / "deploy"),
                push=True,
                mode=mode,
            ),
        )

    @contextlib.contextmanager
    def boot(
        self, pod: str, *, env: str = "dev", mode: Literal["solo", "team"] = "solo"
    ) -> Iterator[TestClient]:
        """A started engine on *pod*'s store and clone, stopped when the block ends."""
        app = create_app(settings=self._settings(pod, env, mode))
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client
        finally:
            _registries.clear()


@pytest.fixture
def deployment(tmp_path: Path) -> _Deployment:
    return _Deployment(tmp_path)


def _sign_in(client: TestClient, username: str, password: str) -> dict[str, str]:
    resp = client.post(_LOGIN, json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _admin_on_its_own_password(client: TestClient) -> tuple[dict[str, str], str]:
    """The admin signed in on a password of its own, past the issued one's forced change."""
    issued = _sign_in(client, "admin", MINTED_ADMIN)
    chosen = secrets.token_urlsafe(16)
    resp = client.post(
        _OWN_CHANGE,
        json={"current_password": MINTED_ADMIN, "new_password": chosen},
        headers=issued,
    )
    assert resp.status_code == 200, resp.text
    held = _sign_in(client, "admin", chosen)
    assert client.get(_ME, headers=held).status_code == 200
    return held, chosen


def _assert_ended(client: TestClient, headers: dict[str, str]) -> None:
    resp = client.get(_ME, headers=headers)
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "session_ended"


class TestLogout:
    @_POD_AFTER
    def test_the_admins_logout_outlives_the_next_start(self, deployment, pod_after):
        with deployment.boot(_FIRST) as client:
            held, _ = _admin_on_its_own_password(client)
            assert client.post(_LOGOUT, headers=held).status_code == 204
            _assert_ended(client, held)

        with deployment.boot(pod_after) as client:
            _assert_ended(client, held)

    @_POD_AFTER
    def test_the_breakglass_logout_outlives_the_next_start(self, deployment, pod_after):
        with deployment.boot(_FIRST) as client:
            held = _sign_in(client, "breakglass", MINTED_BREAKGLASS)
            assert client.post(_LOGOUT, headers=held).status_code == 204

        with deployment.boot(pod_after) as client:
            _assert_ended(client, held)

    def test_a_new_sign_in_after_the_restart_works(self, deployment):
        with deployment.boot(_FIRST) as client:
            held, chosen = _admin_on_its_own_password(client)
            client.post(_LOGOUT, headers=held)

        with deployment.boot(_FIRST) as client:
            fresh = _sign_in(client, "admin", chosen)
            assert client.get(_ME, headers=fresh).status_code == 200


class TestPasswordChangeUnderReview:
    """Production with DFE_GITOPS_MODE=team: main keeps the old hash until the review merges."""

    @_POD_AFTER
    def test_the_admins_change_outlives_the_next_start(self, deployment, pod_after):
        # The first boot commits straight to main, so main carries the password the
        # held token is minted under.
        with deployment.boot(_FIRST) as client:
            held, chosen = _admin_on_its_own_password(client)

        with deployment.boot(_FIRST, env="production", mode="team") as client:
            resp = client.post(
                _OWN_CHANGE,
                json={"current_password": chosen, "new_password": secrets.token_urlsafe(16)},
                headers=held,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["git"]["merged"] is False
            _assert_ended(client, held)

        with deployment.boot(pod_after, env="production", mode="team") as client:
            _assert_ended(client, held)

    def test_the_breakglass_change_outlives_a_restart(self, deployment):
        with deployment.boot(_FIRST, env="production", mode="team") as client:
            held = _sign_in(client, "breakglass", MINTED_BREAKGLASS)
            resp = client.post(
                _OWN_CHANGE,
                json={
                    "current_password": MINTED_BREAKGLASS,
                    "new_password": secrets.token_urlsafe(16),
                },
                headers=held,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["git"]["merged"] is False
            _assert_ended(client, held)

        with deployment.boot(_FIRST, env="production", mode="team") as client:
            _assert_ended(client, held)
