"""Shared fixtures for the LIVE full-stack e2e suite.

These tests run against a REAL deployed DFE (receiver, ClickHouse, HyperDX,
engine API, deploy repo) - not the in-process TestClient suite. They are SKIPPED
unless the relevant DFE_E2E_* env vars point at a live deployment, so they never
break unit/CI runs. No mocks (per MOCKS-POLICY): every assertion hits a real
endpoint.

Configure via env (typically `dfe-engine`-side of a `single`/`standard` deployment):
  DFE_E2E_RECEIVER_URL     https base of the receiver ingest endpoint
  DFE_E2E_RECEIVER_TOKEN   bearer token for the receiver (optional)
  DFE_E2E_CH_HOST/PORT     ClickHouse HTTP endpoint (default port 8123)
  DFE_E2E_CH_USER/PASSWORD/DB   ClickHouse creds + database (default db: dfe)
  DFE_E2E_HYPERDX_URL      HyperDX base URL
  DFE_E2E_HYPERDX_API_KEY  HyperDX API key (optional)
  DFE_E2E_ENGINE_URL       dfe-engine API base
  DFE_E2E_ENGINE_TOKEN     bearer/JWT for the engine API
  DFE_E2E_DEPLOY_REPO_URL  deploy repo (to verify engine git writes)
  DFE_E2E_DEPLOY_REPO_TOKEN / _USER  HTTPS creds for the deploy repo
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass(frozen=True)
class E2EConfig:
    receiver_url: str | None
    receiver_token: str | None
    ch_host: str | None
    ch_port: int
    ch_user: str
    ch_password: str | None
    ch_db: str
    hyperdx_url: str | None
    hyperdx_api_key: str | None
    engine_url: str | None
    engine_token: str | None
    deploy_repo_url: str | None
    deploy_repo_token: str | None
    deploy_repo_user: str
    # Which transform app is deployed for the source under test. Both transforms
    # consume the same topic under their own consumer groups, so with both
    # deployed a row cannot be attributed to either.
    transform: str | None
    # TLS verification for the HTTPS calls. Defaults to False: a deployment
    # typically fronts these with its OWN CA (cert-manager local issuer / internal
    # PKI), and the tests assert the data/control path, not the cert chain. Set
    # DFE_E2E_VERIFY=1 (or a CA bundle path via httpx elsewhere) to enforce it.
    verify: bool


def _cfg() -> E2EConfig:
    return E2EConfig(
        receiver_url=os.getenv("DFE_E2E_RECEIVER_URL"),
        receiver_token=os.getenv("DFE_E2E_RECEIVER_TOKEN"),
        ch_host=os.getenv("DFE_E2E_CH_HOST"),
        ch_port=int(os.getenv("DFE_E2E_CH_PORT", "8123")),
        ch_user=os.getenv("DFE_E2E_CH_USER", "admin"),
        ch_password=os.getenv("DFE_E2E_CH_PASSWORD"),
        ch_db=os.getenv("DFE_E2E_CH_DB", "dfe"),
        hyperdx_url=os.getenv("DFE_E2E_HYPERDX_URL"),
        hyperdx_api_key=os.getenv("DFE_E2E_HYPERDX_API_KEY"),
        engine_url=os.getenv("DFE_E2E_ENGINE_URL"),
        engine_token=os.getenv("DFE_E2E_ENGINE_TOKEN"),
        deploy_repo_url=os.getenv("DFE_E2E_DEPLOY_REPO_URL"),
        deploy_repo_token=os.getenv("DFE_E2E_DEPLOY_REPO_TOKEN"),
        deploy_repo_user=os.getenv("DFE_E2E_DEPLOY_REPO_USER", "dfe"),
        transform=os.getenv("DFE_E2E_TRANSFORM"),
        verify=os.getenv("DFE_E2E_VERIFY", "") not in ("", "0", "false", "False"),
    )


@pytest.fixture(scope="session")
def e2e() -> E2EConfig:
    return _cfg()


def require(cfg: E2EConfig, *attrs: str) -> None:
    """Skip the test unless every named config attr is set."""
    missing = [a for a in attrs if not getattr(cfg, a)]
    if missing:
        pytest.skip(
            f"live e2e not configured: missing {', '.join('DFE_E2E_' + m.upper() for m in missing)}"
        )


def poll_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 60.0,
    interval: float = 2.0,
    desc: str = "condition",
) -> Any:
    """Poll predicate until it returns truthy, or raise on timeout.

    A readiness-gated wait: we poll the real signal (the row exists, the API
    returns it) and use the timeout only as a BACKSTOP for a genuinely-stuck
    dependency - never as the thing we race against. Returns the truthy value.
    """
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:  # dependency not ready yet - keep polling
            last_exc = exc
        time.sleep(interval)
    raise AssertionError(
        f"timed out after {timeout}s waiting for {desc}"
        + (f" (last error: {last_exc})" if last_exc else "")
    )


@pytest.fixture(scope="session")
def ch_client(e2e: E2EConfig):
    """A clickhouse-connect client against the live ClickHouse (native JSON path)."""
    require(e2e, "ch_host")
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=e2e.ch_host,
        port=e2e.ch_port,
        username=e2e.ch_user,
        password=e2e.ch_password or "",
        database=e2e.ch_db,
    )
    yield client
    client.close()
