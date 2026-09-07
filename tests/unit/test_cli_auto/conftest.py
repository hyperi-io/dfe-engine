"""Fixtures for the generated `dfe` CLI tests.

No mocks of internal code: the CLI drives the REAL engine in-process over
``httpx.ASGITransport(app=create_app(...))``. A TestClient context runs the app
lifespan (so registries + auth stores are bootstrapped on ``app.state``); the
same app object then backs the ASGI transport the CLI client uses.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.cli.auto.app import build_root
from dfe_engine.cli.auto.build import GlobalOptions
from dfe_engine.cli.auto.client import Client
from dfe_engine.cli.auto.config import Credential, Store
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    DFESettings,
    HuntsSettings,
    LocalAuthSettings,
    ServicesSettings,
    SourceSettings,
)

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


@dataclass
class Harness:
    """Everything a CLI test needs: the root command, a seeded store, creds."""

    root: Any
    store: Store
    settings: DFESettings
    client_factory: Callable[[str, Any], Client]
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

    def opts(self, **overrides: Any) -> GlobalOptions:
        base = GlobalOptions(store=self.store, client_factory=self.client_factory)
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    def invoke(self, args: list[str], *, obj: GlobalOptions | None = None):
        import io

        options = obj or self.opts()
        # Inject a deterministic sink for generated-command output. pytest's
        # fd-level capture races click's CliRunner buffer for the post-HTTP write,
        # so the injected StringIO makes command output reliable regardless of the
        # capture mode. Built-in / help / error text still lands in result.output,
        # which ``_MergedResult.output`` concatenates.
        sink = io.StringIO()
        options.out_stream = sink
        runner = CliRunner()
        result = runner.invoke(self.root, args, obj=options, catch_exceptions=False)
        return _MergedResult(result, sink.getvalue())


class _MergedResult:
    """Wraps a click Result, merging the injected sink with result.output."""

    def __init__(self, result: Any, sink_text: str) -> None:
        self._result = result
        self._sink_text = sink_text

    @property
    def exit_code(self) -> int:
        return self._result.exit_code

    @property
    def output(self) -> str:
        return self._sink_text + self._result.output

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result, name)


@pytest.fixture
def harness(api_settings: DFESettings, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DFE_CONFIG_HOME", str(tmp_path / "dfe-config-home"))
    # Ensure env creds never leak into resolution during a test.
    for var in ("DFE_API_KEY", "DFE_API_TOKEN", "DFE_API_URL"):
        monkeypatch.delenv(var, raising=False)

    app = create_app(settings=api_settings)
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> None:
        requests.append(request)

    # TestClient is a sync httpx.Client subclass that drives the REAL ASGI app via
    # a portal thread - entering its context runs the app lifespan (registries +
    # auth stores). We inject this same client into the CLI, so CLI commands hit
    # the live in-process engine. (A bare httpx.ASGITransport is async-only and
    # cannot back a sync httpx.Client - hence TestClient.)
    with TestClient(app) as tc:
        tc.event_hooks = {"request": [record]}
        # Seed operator/admin exactly like the API conftest so RBAC works.
        account_store = app.state.account_store
        account_store.reset_password("admin", "test-admin-pw")

        # A real, valid admin API key (groups -> admin role via dfe-admins).
        _, admin_api_key = app.state.api_key_store.create(
            "cli-admin", groups=["dfe-admins"], description="CLI test key"
        )

        admin_token = create_access_token(
            data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]},
            settings=api_settings,
        )

        def client_factory(url: str, credential: Any) -> Client:
            # Reuse the live TestClient (do not close it - Client._owns_session is
            # False when a session is injected).
            return Client(url, credential, session=tc)

        root = build_root(spec=app.openapi())
        store = Store(home=tmp_path / "dfe-config-home")

        yield Harness(
            root=root,
            store=store,
            settings=api_settings,
            client_factory=client_factory,
            admin_token=admin_token,
            admin_api_key=admin_api_key,
            requests=requests,
        )

    _registries.clear()
