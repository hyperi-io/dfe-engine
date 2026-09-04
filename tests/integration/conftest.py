#  Project:      dfe-engine
#  File:         tests/integration/conftest.py
#  Purpose:      Real-ClickHouse connection fixture for integration tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Resolve a real ClickHouse for integration tests, across a tiered preference.

ClickHouse is DFE's only operational store, so integration tests use real CH (no
mocks). The harness picks a target in this preference order:

  Tier 1 "cluster" - a local-network CH CLUSTER (3+ nodes) configured in .env /
      settings, or an explicit ``DFE_TEST_CH_*`` target. Tests replicated behaviour.
  Tier 2 "remote"  - spin a throwaway CH via docker on a REMOTE host over ssh,
      named by ``DFE_TEST_DOCKER_HOST`` (``user@host``). If you can ssh to it you
      can run the container there, so a machine that cannot spare the resources
      offloads to a shared build box -- Kay's Mac hits dfe-engine's limits.
  Tier 3 "local"   - spin a throwaway CH via LOCAL docker.

Controls (env):
  DFE_TEST_TIER=cluster|remote|local (or 1|2|3) - force a tier instead of auto.
      Auto order is cluster -> remote (if DFE_TEST_DOCKER_HOST set) -> local.
  DFE_TEST_DOCKER_HOST=user@host  - the ssh target for tier 2.
  DFE_TEST_KEEP=1                 - keep the spun container running afterwards
      (reused on the next run); default is to remove it (clean up after itself).
      This is the ONLY way a container survives a run.
  DFE_TEST_CH_IMAGE              - override the CH image (default: current LTS).

Containers this suite starts are named `dfe-engine-test-integration-<test>-
clickhouse`, or `dfe-engine-test-integration-clickhouse` for the shared
DFE_TEST_KEEP one, and carry `io.hyperi.test.*` labels naming the suite, the repo,
the service and the owning pid. To sweep whatever a killed run left behind:

  docker rm -f $(docker ps -aq --filter label=io.hyperi.test.suite=dfe-engine-integration)

Every test also DROPs the databases it creates. No Postgres: the hunt runner
coordinates through ClickHouse.
"""

from __future__ import annotations

import os
import subprocess
import time

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


# Container naming and cleanup
# ---------------------------
# A container has to say which repo, which suite and which service it is, so an
# operator looking at `docker ps` can tell what left it behind. A random hex name
# is untraceable the moment one survives, and this suite runs under xdist -n 4 on
# machines that also run the rest of the fleet's tests.
#
#   dfe-engine-test-integration-<test>-clickhouse   throwaway, one per test
#   dfe-engine-test-integration-clickhouse          the DFE_TEST_KEEP instance
#
# The labels answer the questions the name cannot. `owner-pid` is the one that
# matters for a leftover: `ps -p <pid>` says whether that run is still going or
# whether this is rubbish someone can remove.
_SUITE_LABEL = "io.hyperi.test.suite=dfe-engine-integration"


def _container_labels(service: str) -> list[str]:
    """`docker run` label arguments for a container this suite starts."""
    return [
        "--label",
        _SUITE_LABEL,
        "--label",
        "io.hyperi.test.repo=dfe-engine",
        "--label",
        f"io.hyperi.test.service={service}",
        "--label",
        f"io.hyperi.test.owner-pid={os.getpid()}",
    ]


def _container_name(test: str | None, service: str) -> str:
    """Container name for a backing service in this suite.

    ``test`` is the owning test for a throwaway container, or None for the shared
    DFE_TEST_KEEP instance. Docker only accepts ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``,
    and a pytest node id carries brackets and colons from parametrisation, so
    every non-alphanumeric collapses to ``-``.
    """

    def slug(value: str) -> str:
        return "".join(c.lower() if c.isalnum() else "-" for c in value)

    if test is None:
        return f"dfe-engine-test-integration-{slug(service)}"
    return f"dfe-engine-test-integration-{slug(test)}-{slug(service)}"


def _container_running(docker: list[str], name: str) -> bool:
    try:
        return bool(_run([*docker, "ps", "-q", "-f", f"name=^{name}$"], timeout=30).stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _container_exists(docker: list[str], name: str) -> bool:
    try:
        return bool(_run([*docker, "ps", "-aq", "-f", f"name=^{name}$"], timeout=30).stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _reap_stale(docker: list[str], name: str) -> None:
    """Remove a DEAD container holding ``name`` so a leak cannot block this run.

    Only safe for a name that belongs to ONE test. It is deliberately not used on
    the shared DFE_TEST_KEEP name: several xdist workers reach that path at once,
    and a reap there would delete a container a peer had just created but not yet
    started -- see ``_start_or_join_shared``.

    Never touches a RUNNING container. Best-effort otherwise: the create that
    follows reports the real problem.
    """
    if _container_running(docker, name):
        return
    try:
        _run([*docker, "rm", "-f", name], timeout=60)
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


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


def _start_or_join_shared(
    docker: list[str], name: str, run_cmd: list[str], spec: str, attempts: int = 60
) -> None:
    """Make the shared DFE_TEST_KEEP container exist, whoever gets there first.

    The suite runs under xdist, so several workers reach this at the same moment
    for the same name. A check-then-create cannot fix that -- the gap between the
    check and the create IS the race, and the losers get "name is already in use".
    So this loops: use it if it is up, start it if it exists but is stopped (a
    container kept from a previous run), create it if it is absent, and on a
    failed create just go round again and join whoever won.

    Deliberately does not reap: a peer may have created the container a moment
    ago and not started it yet, and removing that would break the run that is
    doing the right thing.
    """
    for _ in range(attempts):
        if _container_running(docker, name):
            return
        if _container_exists(docker, name):
            try:
                _run([*docker, "start", name], timeout=60)
                return
            except (subprocess.SubprocessError, FileNotFoundError):
                time.sleep(0.5)  # someone else is mid-create, or it is going away
                continue
        try:
            _run(run_cmd, timeout=240)  # first image pull can be slow
            return
        except (subprocess.SubprocessError, FileNotFoundError):
            time.sleep(0.5)  # lost the create race; loop and join the winner
    pytest.skip(f"docker host '{spec}' cannot start or join the shared ClickHouse '{name}'")


def _spin_ch_on_docker(spec: str, *, keep: bool, test: str):
    """Start (or reuse) a CH container on a local/remote docker host.

    Returns ``(params, teardown)``. With keep=True a stable-named container is
    shared, reused across runs and left running; otherwise a container named for
    ``test`` is started and removed on teardown. Skips (never errors) if the
    docker host can't run it.
    """
    docker = _docker_prefix(spec)
    image = os.environ.get("DFE_TEST_CH_IMAGE", _DEFAULT_IMAGE)
    # keep=True shares ONE container, so it takes the suite-scoped name. A
    # throwaway is owned by the test that asked for it: the fixture is
    # function-scoped and the suite runs under xdist, so tests do not share one
    # and a common name would collide rather than pool.
    name = _container_name(None if keep else test, "clickhouse")
    run_cmd = [
        *docker,
        "run",
        "-d",
        "--name",
        name,
        *_container_labels("clickhouse"),
        "-p",
        "0:8123",
        "-e",
        "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
        image,
    ]

    if keep:
        _start_or_join_shared(docker, name, run_cmd, spec)
    else:
        # This name belongs to this test alone, so a container holding it is a
        # leak from a killed run and can be removed safely.
        _reap_stale(docker, name)
        try:
            _run(run_cmd, timeout=240)  # first image pull can be slow
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
def ch_params(request):
    """Resolved CH connection params by tier (cluster -> remote -> local docker).

    Yields the params dict (so a test can open MANY clients - e.g. N synthetic
    hunt-runner pods, one client each). Spins/tears down docker as needed.

    Takes ``request`` to name any container after the test that owns it -- pytest
    exposes the node name directly, so nothing has to be passed at the call site.
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
        params, teardown = _spin_ch_on_docker(
            spec, keep=_truthy("DFE_TEST_KEEP"), test=request.node.name
        )

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


@pytest.fixture
def dfe_db(ch_client):
    """An isolated database carrying the REAL default and detection tables.

    Built from the same specs the core schema applies, so a hunt writes into the
    detection table a deployment actually has rather than a stand-in shaped to suit
    the test.
    """
    import uuid

    from dfe_engine.schema.applier import SchemaApplier
    from dfe_engine.schema.ddl_writer import DDLFileWriter
    from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine

    db = f"dfe_core_{uuid.uuid4().hex[:8]}"
    resolver = EngineResolver(client=ch_client)
    applier = SchemaApplier(ch_client, resolver)
    applier.ensure_database(db)
    writer = DDLFileWriter(resolver=resolver, database=db)
    for spec in (writer.default_table_spec(), writer.detection_table_spec()):
        applier.ensure_table(db, spec.name, spec.columns, spec.config)
    try:
        yield db
    finally:
        try:
            # Same ON CLUSTER the applier created with, or a cluster target keeps
            # the database on every node but the one this connection reached.
            on_cluster = resolver.resolve(parse_engine("MergeTree"), db).on_cluster
            ch_client.command(f"DROP DATABASE IF EXISTS `{db}`{on_cluster} SYNC")
        except Exception:
            pass
