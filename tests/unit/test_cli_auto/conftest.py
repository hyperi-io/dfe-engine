"""Fixtures for the `dfe` CLI tests.

No mocks of internal code: the CLI drives the REAL engine in-process. A TestClient
context runs the app lifespan (so registries + auth stores are bootstrapped on
``app.state``), and the CLI sends through a ``Transport`` backed by that client.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from scalo.cli.openapi import Credential, Response, SpecCli, Store
from typer.testing import CliRunner

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.cli.auto.app import build_cli
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    DFESettings,
    HuntsSettings,
    LocalAuthSettings,
    ServicesSettings,
    SourceSettings,
)
from tests.support.accounts import admin_on_its_own_password, session_token

# The admin password these fixtures inject, as a deployment's secret store would.
ADMIN_PASSWORD = "test-admin-pw"


@pytest.fixture
def api_settings(tmp_path: Path) -> DFESettings:
    for name in ("sources", "services", "rules", "hunts", "auth"):
        (tmp_path / name).mkdir()
    return DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            # Production posture refuses to start on the shipped admin password.
            local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
        ),
        api=APISettings(
            jwt_secret="test-secret-key-for-unit-tests-hmac32",
            jwt_expire_minutes=30,
        ),
    )


class InProcessTransport:
    """A ``Transport`` that sends to the in-process engine through a Starlette ``TestClient``."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        credentials: Mapping[str, str],
        content: bytes | None,
        timeout: float,
    ) -> Response:
        reply = self._client.request(
            method, url, headers={**headers, **credentials}, content=content
        )
        return Response(
            reply.status_code,
            {name.lower(): value for name, value in reply.headers.items()},
            reply.content,
            reply.reason_phrase,
        )


@dataclass
class Harness:
    """Everything a CLI test needs: the CLI, a seeded store, creds."""

    cli: SpecCli
    store: Store
    settings: DFESettings
    admin_token: str
    admin_api_key: str
    base_url: str = "http://test"
    requests: list[httpx.Request] = field(default_factory=list)

    def seed_admin(self, profile: str = "default") -> None:
        """Pre-authenticate the store as admin (Bearer token) on a profile."""
        self.store.set_value("url", self.base_url, profile)
        self.store.set_value("account", "admin", profile)
        self.store.save_credential(Credential(account="admin", token=self.admin_token))
        self.store.set_active_profile(profile)

    def invoke(self, args: list[str]):
        """Run ``dfe <args>`` and return the Typer result (``.stdout`` is stdout alone)."""
        return CliRunner().invoke(self.cli.app(args), args, catch_exceptions=False)


@pytest.fixture
def harness(api_settings: DFESettings, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DFE_ENGINE_CONFIG_HOME", str(tmp_path / "dfe-config-home"))
    # Ensure env creds never leak into resolution during a test.
    for var in ("DFE_ENGINE_API_KEY", "DFE_ENGINE_API_TOKEN", "DFE_ENGINE_API_URL"):
        monkeypatch.delenv(var, raising=False)

    app = create_app(settings=api_settings)
    requests: list[httpx.Request] = []

    # TestClient is a sync httpx.Client subclass that drives the REAL ASGI app via
    # a portal thread - entering its context runs the app lifespan (registries +
    # auth stores).
    with TestClient(app) as tc:
        tc.event_hooks = {"request": [requests.append]}
        # Seed the admin exactly like the API conftest so RBAC works.
        admin_on_its_own_password(app)

        # A real, valid admin API key (groups -> admin role via dfe-admins).
        _, admin_api_key = app.state.api_key_store.create(
            "cli-admin", groups=["dfe-admins"], description="CLI test key"
        )

        admin_token = session_token(
            app, api_settings, {"sub": "admin", "org_id": "test-org", "roles": ["admin"]}
        )

        store = Store(tmp_path / "dfe-config-home")
        yield Harness(
            cli=build_cli(app.openapi(), transport=InProcessTransport(tc), store=store),
            store=store,
            settings=api_settings,
            admin_token=admin_token,
            admin_api_key=admin_api_key,
            requests=requests,
        )

    _registries.clear()
