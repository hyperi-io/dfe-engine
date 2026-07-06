#  Project:      dfe-engine
#  File:         tests/integration/conftest.py
#  Purpose:      Real-ClickHouse connection fixture for integration tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Resolve a real ClickHouse for integration tests, across a tiered preference.

ClickHouse is DFE's only operational store, so integration tests use real CH (no
mocks). The harness picks a target in this preference order (per Derek):

  Tier 1 "cluster" - a local-network CH CLUSTER (3+ nodes) configured in .env /
      settings, or an explicit ``DFE_TEST_CH_*`` target. Tests replicated behaviour.
  Tier 2 "remote"  - spin a throwaway CH via docker on a REMOTE host over ssh,
      named by ``DFE_TEST_DOCKER_HOST`` (e.g. derek@desktop-derek.devex.hyperi.io).
      The rule: "if I can ssh to it, I can move my docker tests there" - so a
      resource-constrained laptop (Kay's Mac hits dfe-engine limits) offloads the
      container to a shared build box.
  Tier 3 "local"   - spin a throwaway CH via LOCAL docker.

Controls (env):
  DFE_TEST_TIER=cluster|remote|local (or 1|2|3) - force a tier instead of auto.
      Auto order is cluster -> remote (if DFE_TEST_DOCKER_HOST set) -> local.
  DFE_TEST_DOCKER_HOST=user@host  - the ssh target for tier 2.
  DFE_TEST_KEEP=1                 - keep the spun container running afterwards
      (reused on the next run); default is to remove it (clean up after itself).
  DFE_TEST_CH_IMAGE              - override the CH image (default: current LTS).

Every test also DROPs the databases it creates. No Postgres: the hunt runner
coordinates through ClickHouse.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import pytest

# Current ClickHouse LTS line (see standards/languages/SQL-CLICKHOUSE.md).
_DEFAULT_IMAGE = "clickhouse/clickhouse-server:25.8"

# Hard memory cap for the throwaway test container. Derek's standard: NEVER run CH
# uncapped on a working laptop - and CH for tests needs very little RAM. CH 24.x+
# honours the container cgroup limit (auto-sets max_server_memory_usage), so this
# --memory cap is the control. Override with DFE_TEST_CH_MEMORY (e.g. "1500m").
_CH_TEST_MEMORY = os.environ.get("DFE_TEST_CH_MEMORY", "1g")


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _cluster_params() -> dict | None:
    """Tier 1: a configured CH cluster (settings/.env non-localhost, then DFE_TEST_CH_*)."""
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


def _resolve_tier() -> str:
    """Chosen tier: explicit DFE_TEST_TIER, else auto (cluster -> remote -> local)."""
    forced = os.environ.get("DFE_TEST_TIER", "").strip().lower()
    if forced in ("cluster", "1"):
        return "cluster"
    if forced in ("remote", "2"):
        return "remote"
    if forced in ("local", "3"):
        return "local"
    if _cluster_params() is not None:
        return "cluster"
    if os.environ.get("DFE_TEST_DOCKER_HOST"):
        return "remote"
    return "local"


def _docker_prefix(spec: str) -> list[str]:
    """The docker CLI prefix for a local host or an ssh:// remote docker host."""
    if spec in ("local", "localhost", "127.0.0.1"):
        return ["docker"]
    return ["ssh", spec.removeprefix("ssh://"), "docker"]


def _reach_address(spec: str) -> str:
    """Address to reach the published port on (localhost, or the ssh host)."""
    if spec in ("local", "localhost", "127.0.0.1"):
        return "127.0.0.1"
    return spec.removeprefix("ssh://").split("@")[-1]


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _spin_ch_on_docker(spec: str, *, keep: bool):
    """Start (or reuse) a CH container on a local/remote docker host.

    Returns ``(params, teardown)``. With keep=True a stable-named container is
    reused across runs and left running; otherwise a unique container is started
    and removed on teardown. Skips (never errors) if the docker host can't run it.
    """
    docker = _docker_prefix(spec)
    image = os.environ.get("DFE_TEST_CH_IMAGE", _DEFAULT_IMAGE)
    name = "dfe-ch-test" if keep else f"dfe-ch-test-{uuid.uuid4().hex[:8]}"

    already_running = False
    if keep:
        try:
            already_running = bool(
                _run([*docker, "ps", "-q", "-f", f"name=^{name}$"], timeout=30).stdout.strip()
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            already_running = False

    if not already_running:
        try:
            _run(
                [
                    *docker,
                    "run",
                    "-d",
                    "--name",
                    name,
                    # Hard memory cap (Derek: never run CH uncapped on a laptop);
                    # --memory-swap == --memory disables swap beyond the cap.
                    "--memory",
                    _CH_TEST_MEMORY,
                    "--memory-swap",
                    _CH_TEST_MEMORY,
                    "-p",
                    "0:8123",
                    "-e",
                    "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
                    image,
                ],
                timeout=240,  # first image pull can be slow
            )
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            pytest.skip(f"docker host '{spec}' cannot start ClickHouse: {exc}")

    def teardown() -> None:
        if keep:
            return  # left running for reuse (DFE_TEST_KEEP)
        try:
            _run([*docker, "rm", "-f", name], timeout=60)
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    try:
        # docker prints e.g. "0.0.0.0:49153" (take the last line, then the port)
        out = _run([*docker, "port", name, "8123"], timeout=30).stdout.strip()
        port = int(out.splitlines()[-1].rsplit(":", 1)[-1])
    except (subprocess.SubprocessError, ValueError, IndexError) as exc:
        teardown()
        pytest.skip(f"could not resolve the ClickHouse port on '{spec}': {exc}")

    params = {
        "host": _reach_address(spec),
        "port": port,
        "username": "default",
        "password": "",
        "secure": False,
    }
    return params, teardown


def _connect_when_ready(clickhouse_connect, params: dict, attempts: int = 40, delay: float = 1.0):
    """Poll the real readiness signal (SELECT 1) until it succeeds; return the client."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            client = clickhouse_connect.get_client(**params)
            client.command("SELECT 1")
            return client
        except Exception as exc:  # CH still starting up
            last = exc
            time.sleep(delay)
    raise last if last is not None else RuntimeError("ClickHouse unreachable")


@pytest.fixture
def ch_params():
    """Resolved CH connection params by tier (cluster -> remote -> local docker).

    Yields the params dict (so a test can open MANY clients - e.g. N synthetic
    hunt-runner pods, one client each). Spins/tears down docker as needed.
    """
    clickhouse_connect = pytest.importorskip("clickhouse_connect")
    tier = _resolve_tier()
    teardown = None

    if tier == "cluster":
        params = _cluster_params()
        if params is None:
            pytest.skip("DFE_TEST_TIER=cluster but no CH cluster configured (.env / DFE_TEST_CH_*)")
    else:
        if tier == "remote":
            spec = os.environ.get("DFE_TEST_DOCKER_HOST")
            if not spec:
                pytest.skip("DFE_TEST_TIER=remote but DFE_TEST_DOCKER_HOST (user@host) is not set")
        else:
            spec = "local"
        params, teardown = _spin_ch_on_docker(spec, keep=_truthy("DFE_TEST_KEEP"))

    try:
        _connect_when_ready(clickhouse_connect, params).close()  # verify reachable
    except Exception as exc:  # not reachable -> skip, do not error the suite
        if teardown is not None:
            teardown()
        pytest.skip(f"ClickHouse not reachable: {exc}")
    try:
        yield params
    finally:
        if teardown is not None:
            teardown()


@pytest.fixture
def ch_client(ch_params):
    """A single real ClickHouse client (from ch_params)."""
    import clickhouse_connect

    return clickhouse_connect.get_client(**ch_params)


# ---------------------------------------------------------------------------
# CANON: the 3-target CH integration matrix (local / cluster / cloud).
#
# Derek: "we OFTEN find breakages moving single node -> cluster -> cloud". So the
# CH integration fixtures are PARAMETRISED over all three targets - every test that
# asks for `clickhouse_client` / `clickhouse_test_database` runs once per PRESENT
# target. An absent or unreachable target SKIPs (visibly), never fails, so CI (or a
# laptop) with only local docker gracefully exercises the single-node path while
# the run still flags what it could not cover. Where a scale target IS present the
# same test does proper scale verification (Replicated on cluster, Shared on Cloud).
#
# Targets read the STANDARD connection env (no test-only var names):
#   local   - a CI-aligned THROWAWAY single-node CH spun in local docker and ERASED
#             after the run (keeperless -> MergeTree); a 1:1 match for CI, kept
#             SEPARATE from any always-on dev daemon (never touched). DFE_TEST_KEEP=1
#             reuses it; skips if docker is unavailable.
#   cluster - the primary DFE_CLICKHOUSE_* connection when it is a real (non-localhost)
#             multi-node cluster (-> ReplicatedMergeTree). Present when
#             DFE_CLICKHOUSE_HOST is non-localhost and answers SELECT 1.
#   cloud   - the DFE_CLICKHOUSE_CLOUD_* SQL connection (-> SharedMergeTree). Present
#             when DFE_CLICKHOUSE_CLOUD_HOST is set + reachable. A stopped Cloud
#             service just fails the probe and skips - never woken here (billable).
#
# See .env.example for the block. This matrix is the canonical way to test all CH
# work - see the project memory `project_ch_integration_canon`.
# ---------------------------------------------------------------------------

_MATRIX_TARGETS = ("local", "cluster", "cloud")


def _cluster_conn() -> dict | None:
    """The primary DFE_CLICKHOUSE_* connection, when it is a real (non-localhost) target."""
    host = os.environ.get("DFE_CLICKHOUSE_HOST", "")
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("DFE_CLICKHOUSE_PORT", "8123")),
        "username": os.environ.get(
            "DFE_CLICKHOUSE_USERNAME", os.environ.get("DFE_CLICKHOUSE_USER", "default")
        ),
        "password": os.environ.get("DFE_CLICKHOUSE_PASSWORD", ""),
        "secure": _truthy("DFE_CLICKHOUSE_SECURE"),
        "verify": _truthy("DFE_CLICKHOUSE_VERIFY"),
    }


def _cloud_conn() -> dict | None:
    """The DFE_CLICKHOUSE_CLOUD_* SQL (data-plane) connection, or None if unconfigured."""
    host = os.environ.get("DFE_CLICKHOUSE_CLOUD_HOST", "")
    if not host:
        return None
    # A *.clickhouse.cloud endpoint is TLS; default secure, honour an explicit "false".
    secure = os.environ.get("DFE_CLICKHOUSE_CLOUD_SECURE", "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    return {
        "host": host,
        "port": int(os.environ.get("DFE_CLICKHOUSE_CLOUD_PORT", "8443")),
        "username": os.environ.get("DFE_CLICKHOUSE_CLOUD_USERNAME", "default"),
        "password": os.environ.get("DFE_CLICKHOUSE_CLOUD_PASSWORD", ""),
        "secure": secure,
        "verify": _truthy("DFE_CLICKHOUSE_CLOUD_VERIFY"),
    }


@pytest.fixture(params=_MATRIX_TARGETS, scope="session")
def ch_conn(request):
    """Resolve + probe ONE matrix target (local/cluster/cloud); skip if absent/unreachable.

    The parametrisation point for the whole CH integration matrix; yields the
    connection params plus an ``id`` (the target name). local spins a fresh,
    MEMORY-CAPPED single-node CH in local docker (erased afterwards); cluster is the
    primary DFE_CLICKHOUSE_* target and cloud is DFE_CLICKHOUSE_CLOUD_* - both skip
    when unset or unreachable (a stopped Cloud service is never woken here).
    """
    clickhouse_connect = pytest.importorskip("clickhouse_connect")
    target = request.param
    teardown = None
    if target == "local":
        # CI-aligned throwaway: a fresh capped CH, removed on teardown (or kept
        # across runs with DFE_TEST_KEEP=1). Skips cleanly if docker is unavailable.
        params, teardown = _spin_ch_on_docker("local", keep=_truthy("DFE_TEST_KEEP"))
        try:
            _connect_when_ready(clickhouse_connect, params).close()  # wait for startup
        except Exception as exc:
            if teardown is not None:
                teardown()
            pytest.skip(f"CH target {target!r} not reachable: {exc}")
    else:
        if target == "cloud" and not _truthy("DFE_TEST_CLOUD"):
            # Cloud is billable and probing an IDLE service auto-wakes it, so it is
            # OFF by default even when configured. Opt in deliberately.
            pytest.skip("cloud target off by default (billable; set DFE_TEST_CLOUD=1 to enable)")
        params = _cluster_conn() if target == "cluster" else _cloud_conn()
        if params is None:
            var = (
                "DFE_CLICKHOUSE_HOST (non-localhost)"
                if target == "cluster"
                else "DFE_CLICKHOUSE_CLOUD_HOST"
            )
            pytest.skip(f"CH target {target!r} not configured (set {var})")
        try:
            # Short probe: an unreachable cluster/cloud (VPN down, service stopped)
            # skips fast rather than hanging the suite.
            probe = clickhouse_connect.get_client(connect_timeout=5, **params)
            probe.command("SELECT 1")
            probe.close()
        except Exception as exc:
            pytest.skip(f"CH target {target!r} not reachable: {exc}")
    try:
        yield {"id": target, **params}
    finally:
        if teardown is not None:
            teardown()


def _sensed_cluster_name(client) -> str | None:
    """The ON CLUSTER name for a multi-node target (the 'cluster' macro, else the
    first multi-host cluster in system.clusters); None on a single node."""
    try:
        rows = client.query(
            "SELECT substitution FROM system.macros WHERE macro = 'cluster'"
        ).result_rows
        if rows and rows[0][0]:
            return rows[0][0]
        rows = client.query(
            "SELECT cluster FROM system.clusters GROUP BY cluster "
            "HAVING count() > 1 ORDER BY cluster LIMIT 1"
        ).result_rows
        return rows[0][0] if rows else None
    except Exception:
        return None


@pytest.fixture(scope="session")
def clickhouse_test_database(ch_conn, test_run_id):
    """Isolated CH database on the current matrix target (overrides the root fixture).

    Parametrised via ch_conn, so every integration test asking for
    `clickhouse_client` / `clickhouse_test_database` runs once per present target. On
    a multi-node cluster the database is created ON CLUSTER so it exists on EVERY node
    - a plain CREATE DATABASE would leave the subsequent ON CLUSTER table creates
    failing on the other replicas (the classic single -> cluster trap).
    """
    import clickhouse_connect

    db_name = f"dfe_test_{ch_conn['id']}_{test_run_id}"
    conn = {k: v for k, v in ch_conn.items() if k != "id"}
    client = clickhouse_connect.get_client(**conn)
    on_cluster = ""
    if ch_conn["id"] == "cluster":
        name = _sensed_cluster_name(client)
        if name:
            on_cluster = f" ON CLUSTER {name}"
    client.command(f"CREATE DATABASE IF NOT EXISTS {db_name}{on_cluster}")
    try:
        yield db_name
    finally:
        try:
            client.command(f"DROP DATABASE IF EXISTS {db_name}{on_cluster}")
        except Exception:  # best-effort cleanup - a drop failure must not fail the run
            pass


@pytest.fixture
def clickhouse_client(ch_conn, clickhouse_test_database):
    """A real ClickHouse client on the current matrix target + isolated db.

    Overrides the single-target root fixture; parametrised via ch_conn.
    """
    import clickhouse_connect

    conn = {k: v for k, v in ch_conn.items() if k != "id"}
    return clickhouse_connect.get_client(database=clickhouse_test_database, **conn)
