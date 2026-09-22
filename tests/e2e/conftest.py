#  Project:      dfe-engine
#  File:         tests/e2e/conftest.py
#  Purpose:      Shared fixtures and config for the live full-stack e2e suite
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
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
  DFE_E2E_ENGINE_USER/_PASSWORD  local login the flow suite mints its own token
                           from, because a flow run outlives one token
  DFE_E2E_TRANSPORT        the data path the flow suite must prove:
                           kafka | grpc | both
  DFE_E2E_DEPLOY_REPO_URL  deploy repo (to verify engine git writes)
  DFE_E2E_DEPLOY_REPO_TOKEN / _USER  HTTPS creds for the deploy repo
  DFE_E2E_TRANSFORM        which transform app is deployed for the source under
                           test (dfe-transform-vrl | dfe-transform-vector)
  DFE_E2E_VERIFY           1 to enforce TLS verification (default off)

OIDC fixture logins are separate, because they name a shared test identity the
whole suite reuses rather than one deployment's endpoint:
  DFE_OIDC_FIXTURE_USER      default dfe-test@dfe-oidc.test
  DFE_OIDC_FIXTURE_PASSWORD  no default; pre-shared into .env
  DFE_OIDC_<PROVIDER>_FIXTURE_USER / _PASSWORD  per-provider override, <PROVIDER>
                             uppercased as /api/v1/auth/setup-status names it
"""

from __future__ import annotations

import json
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
    engine_user: str
    engine_password: str | None
    # The data path the flow suite must prove, in the deployment's own words
    # (kafka | grpc | both). Empty means nothing asked, so the flow suite gates.
    transport: str
    deploy_repo_url: str | None
    deploy_repo_token: str | None
    deploy_repo_user: str
    # Which transform app is deployed for the source under test. Declared, not
    # observed: both transforms consume the source topic under their own consumer
    # groups and emit to the same one, so no row identifies its producer.
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
        engine_user=os.getenv("DFE_E2E_ENGINE_USER", "admin"),
        engine_password=os.getenv("DFE_E2E_ENGINE_PASSWORD"),
        transport=os.getenv("DFE_E2E_TRANSPORT", ""),
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


def must[T](value: T | None) -> T:
    """Narrow a config attr that ``require`` has already gated on.

    ``require`` skips the test when the attr is unset, but a type checker cannot
    see that across the call, so every use site reads as ``str | None``.

    The message matters: several call sites sit inside ``poll_until`` predicates,
    which swallow every exception and retry, so a bare AssertionError would
    surface as an empty ``(last error: )`` after the full timeout.
    """
    assert value is not None, "require() should have skipped this test before now"
    return value


# The shared throwaway identity every provider's fixture account carries.
OIDC_FIXTURE_DEFAULT_USER = "dfe-test@dfe-oidc.test"


@dataclass(frozen=True)
class OIDCFixtureLogin:
    user: str
    password: str


def oidc_fixture_user(provider: str) -> str:
    """Resolve the fixture username: per-provider override, generic, then default."""
    return (
        os.getenv(f"DFE_OIDC_{provider.upper()}_FIXTURE_USER")
        or os.getenv("DFE_OIDC_FIXTURE_USER")
        or OIDC_FIXTURE_DEFAULT_USER
    )


def oidc_fixture_password(provider: str) -> str | None:
    """Resolve the fixture password: per-provider override, generic, then unset."""
    return (
        os.getenv(f"DFE_OIDC_{provider.upper()}_FIXTURE_PASSWORD")
        or os.getenv("DFE_OIDC_FIXTURE_PASSWORD")
        or None
    )


@pytest.fixture(scope="session")
def oidc_fixture_login() -> Callable[[str], OIDCFixtureLogin]:
    """Fixture login for a provider, SKIPPING when no password resolves.

    Skip rather than fail: the password is pre-shared into a tester's `.env` and
    never committed, so an unconfigured provider means the run cannot exercise
    that IdP - not that the deployment is broken.
    """

    def _login(provider: str) -> OIDCFixtureLogin:
        password = oidc_fixture_password(provider)
        if not password:
            pytest.skip(
                f"no OIDC fixture password for {provider}: set "
                f"DFE_OIDC_{provider.upper()}_FIXTURE_PASSWORD or DFE_OIDC_FIXTURE_PASSWORD"
            )
        return OIDCFixtureLogin(user=oidc_fixture_user(provider), password=password)

    return _login


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


# A source write recompiles the receiver's ConfigMap and the deployment rolls on the
# new checksum, so how long a post gives the replacement to come back.
INGEST_RETRY_WINDOW = 180.0


def _json_body(payload: Any) -> bytes:
    """Serialise exactly as httpx's own ``json=`` would.

    One record and a batch of them must reach the receiver in the same encoding,
    or the only difference the batch cases prove is the serialiser's.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _post(cfg: E2EConfig, body: bytes, content_type: str) -> None:
    """POST one prepared body to the deployment's ingest endpoint.

    Shared because both live suites send the same way, and a second copy would be
    a second place for the token header or the TLS posture to drift. Callers gate
    on ``require(cfg, "receiver_url")`` first.

    A connection dropped mid-roll, or a 5xx from a pod on its way out, is retried
    until ``INGEST_RETRY_WINDOW`` runs out, because rolling on a routing change is
    the behaviour under test. A 4xx the receiver answers with still fails on the
    spot: that is a rejection of the record, not of the moment.
    """
    import httpx

    url = must(cfg.receiver_url)
    headers = {"Content-Type": content_type}
    if cfg.receiver_token:
        headers["Authorization"] = f"Bearer {cfg.receiver_token}"
    deadline = time.monotonic() + INGEST_RETRY_WINDOW
    while True:
        try:
            with httpx.Client(verify=cfg.verify, timeout=30.0) as client:
                response = client.post(url, content=body, headers=headers)
        except httpx.TransportError as exc:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"the receiver never answered within {INGEST_RETRY_WINDOW}s: {exc}"
                ) from exc
            time.sleep(2.0)
            continue
        if response.status_code >= 500:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"the receiver kept failing for {INGEST_RETRY_WINDOW}s: "
                    f"{response.status_code} {response.text}"
                )
            time.sleep(2.0)
            continue
        assert response.status_code < 300, f"receiver rejected the event: {response.text}"
        break


def post_events(cfg: E2EConfig, bodies: list[dict]) -> None:
    """POST each body to the deployment's ingest endpoint, one request per record."""
    for body in bodies:
        _post(cfg, _json_body(body), "application/json")


def batch_body(bodies: list[dict]) -> bytes:
    """Every record as ONE top-level JSON array, the way a batching client sends."""
    return _json_body(bodies)


def ndjson_body(bodies: list[dict]) -> bytes:
    """Every record as ONE newline-delimited body, one complete value per line."""
    return b"\n".join(_json_body(body) for body in bodies)


def post_batch(cfg: E2EConfig, bodies: list[dict]) -> None:
    """POST every body as one JSON array request.

    Same endpoint, headers and retry window as ``post_events``, because the body
    shape is the only thing under test here.
    """
    _post(cfg, batch_body(bodies), "application/json")


def post_ndjson(cfg: E2EConfig, bodies: list[dict]) -> None:
    """POST every body as one newline-delimited request.

    The other batch shape the ingest endpoint advertises, so it carries the same
    one-row-per-record obligation as an array.
    """
    _post(cfg, ndjson_body(bodies), "application/x-ndjson")


def cluster_name(ch_client) -> str:
    """The cluster this server declares, or empty on a single node.

    Every DDL a test applies has to reach every replica, so this is read rather
    than configured: a statement without ON CLUSTER lands on the one replica the
    connection reached and the others never see it.
    """
    try:
        rows = ch_client.query(
            "SELECT substitution FROM system.macros WHERE macro = 'cluster'"
        ).result_rows
    except Exception:
        return ""
    return str(rows[0][0]) if rows else ""


def drop_table(ch_client, db: str, name: str) -> None:
    """DROP a table on every replica of the cluster the server declares.

    A drop without ON CLUSTER lands on the one replica the connection reached,
    and the next CREATE IF NOT EXISTS ON CLUSTER then makes an orphan with its own
    replication path there, so a third of the writes and reads go to it.
    """
    cluster = cluster_name(ch_client)
    on_cluster = f" ON CLUSTER {cluster} SYNC" if cluster else ""
    ch_client.command(f"DROP TABLE IF EXISTS {db}.`{name}`{on_cluster}")


# ClickHouse says one of these when the table or database is simply not there
# yet, which is the only absence a poll should read as "no rows".
NOT_THERE_YET = ("UNKNOWN_TABLE", "UNKNOWN_DATABASE", "does not exist", "doesn't exist")


def count_rows(
    ch_client,
    table: str,
    *,
    where: str = "",
    marker: str | None = None,
    contains: str | None = None,
) -> int:
    """Rows in *table*, narrowed by a WHERE, this run's marker and/or a substring.

    Answers 0 only while the table or database does not exist yet. Every other
    error is raised: a query that cannot run - an unknown column, a function the
    column's type will not take - otherwise reads as an empty table forever, and
    a test that can only report zero proves nothing.

    The marker is matched on ``_raw``, the String copy of the payload. ``_json``
    holds the same bytes as the ClickHouse JSON type, which LIKE refuses, and a
    typed source table carries neither - such a table is counted with a delta
    instead.

    ``contains`` is a second ``_raw`` substring, bound the same way. A caller that
    wrote the LIKE into *where* by hand would put a bare ``%`` into the SQL, which
    clickhouse-connect then reads as its own parameter placeholder.
    """
    clauses = [
        c
        for c in (where, "_raw LIKE %(m)s" if marker else "", "_raw LIKE %(c)s" if contains else "")
        if c
    ]
    parameters: dict[str, str] = {}
    if marker:
        parameters["m"] = f"%{marker}%"
    if contains:
        parameters["c"] = f"%{contains}%"
    sql = f"SELECT count() FROM {table}" + (f" WHERE {' AND '.join(clauses)}" if clauses else "")
    try:
        result = ch_client.query(sql, parameters=parameters or None)
    except Exception as exc:
        if any(text in str(exc) for text in NOT_THERE_YET):
            return 0
        raise
    return int(result.result_rows[0][0]) if result.result_rows else 0


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
