#  Project:      dfe-engine
#  File:         tests/integration/conftest.py
#  Purpose:      Connection fixtures for integration tests (real PG / CH)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Resolve real DB connections for integration tests, cleaning up after.

ClickHouse resolution order (per Derek): (1) the .env / settings-configured CH
(e.g. the devex pet cluster) if a real host is set; (2) an explicit DFE_TEST_CH_*
env (used to point at a docker deployment - local or the remote docker host);
(3) a docker fallback via testcontainers if available; else skip. The CH tests DROP
everything they create (clean up after themselves).

Postgres (for the hunt-runner claim table) resolves from DFE_TEST_PG_DSN, then a
testcontainers docker fallback, else skip.
"""

from __future__ import annotations

import os

import pytest


def _ch_conn_params() -> dict | None:
    """Resolve CH connection: settings/.env first, then DFE_TEST_CH_* env."""
    try:
        from dfe_engine.settings import load_settings

        s = load_settings().clickhouse
        if s.host and s.host not in ("localhost", "127.0.0.1"):
            return {
                "host": s.host,
                "port": s.port,
                "username": s.username,
                "password": s.password,
                "secure": s.secure,
            }
    except Exception:
        pass
    host = os.environ.get("DFE_TEST_CH_HOST")
    if host:
        return {
            "host": host,
            "port": int(os.environ.get("DFE_TEST_CH_PORT", "8123")),
            "username": os.environ.get("DFE_TEST_CH_USER", "default"),
            "password": os.environ.get("DFE_TEST_CH_PASSWORD", ""),
            "secure": False,
        }
    return None


@pytest.fixture
def ch_client():
    """A real ClickHouse client (settings/.env -> DFE_TEST_CH_* -> docker -> skip)."""
    clickhouse_connect = pytest.importorskip("clickhouse_connect")
    params = _ch_conn_params()
    container = None
    if params is None:
        # docker fallback (CI / hosts with docker): auto spin + teardown
        tc = pytest.importorskip("testcontainers.clickhouse")
        container = tc.ClickHouseContainer("clickhouse/clickhouse-server:24.8")
        container.start()
        params = {
            "host": container.get_container_host_ip(),
            "port": int(container.get_exposed_port(8123)),
            "username": "default",
            "password": "",
            "secure": False,
        }
    try:
        client = clickhouse_connect.get_client(**params)
        client.command("SELECT 1")  # fail fast if unreachable
    except Exception as exc:  # not reachable -> skip, don't error the suite
        if container is not None:
            container.stop()
        pytest.skip(f"ClickHouse not reachable: {exc}")
    try:
        yield client
    finally:
        if container is not None:
            container.stop()


@pytest.fixture
def pg_dsn():
    """A real PostgreSQL DSN (DFE_TEST_PG_DSN -> docker fallback -> skip)."""
    pytest.importorskip("psycopg")
    dsn = os.environ.get("DFE_TEST_PG_DSN")
    container = None
    if not dsn:
        tc = pytest.importorskip("testcontainers.postgres")
        container = tc.PostgresContainer("postgres:16")
        container.start()
        dsn = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    try:
        yield dsn
    finally:
        if container is not None:
            container.stop()


@pytest.fixture
def pg_conn(pg_dsn):
    """A single real PostgreSQL connection from pg_dsn."""
    import psycopg

    try:
        conn = psycopg.connect(pg_dsn, autocommit=False)
    except Exception as exc:
        pytest.skip(f"Postgres not reachable: {exc}")
    try:
        yield conn
    finally:
        conn.close()
