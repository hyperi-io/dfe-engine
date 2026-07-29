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

# Current ClickHouse LTS line, and the version dfe-infra actually deploys
# (versions.yaml). LTS ships twice a year with a year of support, so 25.8 is the
# PREVIOUS one -- testing a release older than production is the wrong direction
# for a datastore whose defaults move between majors.
#
# Tag on its own line, separate from the image name, so one Renovate regex
# covers every language in the fleet.
# renovate: datasource=docker depName=clickhouse/clickhouse-server
_CH_TAG = "26.3"
_DEFAULT_IMAGE = f"clickhouse/clickhouse-server:{_CH_TAG}"


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
