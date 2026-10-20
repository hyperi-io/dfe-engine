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

  Tier 1 "cluster" - an explicit ``DFE_TEST_CH_*`` target, or, only under
      ``DFE_TEST_TIER=cluster``, the CH cluster the app settings (.env) name. A run
      never takes a ClickHouse host from .env unasked. Tests replicated behaviour.
  Tier 2 "remote"  - spin a throwaway CH via docker on a REMOTE host over ssh,
      named by ``DFE_TEST_DOCKER_HOST`` (``user@host``). If you can ssh to it you
      can run the container there, so a machine that cannot spare the resources
      offloads to a shared build box -- Kay's Mac hits dfe-engine's limits.
  Tier 3 "local"   - spin a throwaway CH via LOCAL docker.

Controls (env):
  DFE_TEST_TIER=cluster|remote|local (or 1|2|3) - force a tier instead of auto.
      Auto order is cluster -> remote (if DFE_TEST_DOCKER_HOST set) -> local.
  DFE_TEST_CH_HOST, _PORT, _USER, _PASSWORD - the tier 1 target, which wins over
      anything the app settings name.
  DFE_TEST_DOCKER_HOST=user@host  - the ssh target for tier 2.
  DFE_TEST_KEEP=1                 - keep the spun container running afterwards
      (reused on the next run); default is to remove it (clean up after itself).
      This is the ONLY way a container survives a run.
  DFE_TEST_CH_IMAGE              - override the CH image (default: current LTS).

Containers this suite starts are named `dfe-engine-test-integration-<test>-
clickhouse`, or `dfe-engine-test-integration-clickhouse` for the shared
DFE_TEST_KEEP one, and carry `io.hyperi.test.*` labels naming the suite, the repo,
the service and the owning pid. To sweep whatever a killed run left behind:

  docker rm -f -v $(docker ps -aq --filter label=io.hyperi.test.suite=dfe-engine-integration)

Every test also DROPs the databases it creates. No Postgres: the hunt runner
coordinates through ClickHouse.

Every container this suite starts runs Keeper embedded, with the ``{cluster}``,
``{shard}`` and ``{replica}`` macros and a one-node ``remote_servers`` cluster:
every DFE table is an argumentless ``ReplicatedMergeTree`` inside a Replicated
database, which a server without them refuses (dfe-schemas
``docs/clickhouse-platform.md``). A configured cluster target must carry the same,
and keep its SQL users and roles in Keeper.

The document store resolves separately (``mongo_uri``): ``DFE_TEST_MONGO_URI`` if
set, else one FerretDB per xdist worker on the same docker host, named
`dfe-engine-test-integration-<worker>-<pid>-ferretdb` and removed at session end.
DFE_TEST_KEEP does not apply to it.

The Kafka broker resolves the same way (``kafka_bootstrap``): ``DFE_TEST_KAFKA_BOOTSTRAP``
if set, else one KRaft broker per xdist worker on local docker, named
`dfe-engine-test-integration-<worker>-<pid>-kafka` and removed at session end.
"""

import base64
import functools
import os
import secrets
import shlex
import subprocess
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from tests.support.clickhouse_cleanup import drop_database_and_policies
from tests.support.producer_contract import in_ci

# Current ClickHouse LTS line, and the version dfe-infra actually deploys
# (versions.yaml). LTS ships twice a year with a year of support, so 25.8 is the
# PREVIOUS one -- testing a release older than production is the wrong direction
# for a datastore whose defaults move between majors.
#
# Tag on its own line, separate from the image name, so one Renovate regex
# covers every language in the fleet. The digest is dfe-infra versions.yaml's
# services-digests pin for the same tag, so a retag cannot change what runs.
# renovate: datasource=docker depName=clickhouse/clickhouse-server
_CH_TAG = "26.3.42.3"
_CH_DIGEST = "sha256:21d572843e59539c7d100286b6f5a6053c341fe4b33c2d7b74b1ff7cb24c5399"
_DEFAULT_IMAGE = f"clickhouse/clickhouse-server:{_CH_TAG}@{_CH_DIGEST}"

# The document store production runs (dfe-infra versions.yaml); the eval image
# bundles its PostgreSQL + DocumentDB backend into one container. versions.yaml
# pins the production image, not this one, so the digest is ghcr's index for the tag.
# renovate: datasource=docker depName=ghcr.io/ferretdb/ferretdb-eval
_FERRETDB_TAG = "2.7.0"
_FERRETDB_DIGEST = "sha256:1bf47a449dd65839aabfc1a535d1370c98326f8a90de20437eda0aeb30bd8dd5"
_FERRETDB_IMAGE = f"ghcr.io/ferretdb/ferretdb-eval:{_FERRETDB_TAG}@{_FERRETDB_DIGEST}"
_FERRETDB_USER = "dfe"

# The broker dfe-infra runs on the docker path, at the digest versions.yaml pins for the tag.
# renovate: datasource=docker depName=apache/kafka
_KAFKA_TAG = "4.3.1"
_KAFKA_DIGEST = "sha256:77e3df9054047a88b520d0cc46e16696d3b22022e1d580aeccd2632df6532837"
_KAFKA_IMAGE = f"apache/kafka:{_KAFKA_TAG}@{_KAFKA_DIGEST}"

# The cluster name the {cluster} macro carries on every container this suite starts.
_CLUSTER = "dfe"
_KEEPER_PORT = 9181
_KEEPER_CONFIG_PATH = "/etc/clickhouse-server/config.d/dfe-keeper.xml"


# SQL users and roles in Keeper. It must REPLACE the default list: appended, it sits
# behind local_directory, and the first writable storage takes every CREATE ROLE.
_REPLICATED_ACCESS = """
    <user_directories replace="replace">
        <users_xml><path>users.xml</path></users_xml>
        <replicated><zookeeper_path>/clickhouse/access/</zookeeper_path></replicated>
    </user_directories>"""


def keeper_config(
    *, replica: str, keeper_host: str | None, hosts: list[str], replicated_access: bool = False
) -> str:
    """The server config one replica needs to hold DFE's Replicated database.

    Args:
        replica: The ``{replica}`` macro, unique per server.
        keeper_host: The host this server runs Keeper on and every replica connects
            to, or None when this server only connects to another one's.
        hosts: Every replica's hostname, for the one-shard ``remote_servers`` cluster.
        replicated_access: Keep SQL users and roles in Keeper, which a cluster of
            more than one server needs. A single node may keep them local.
    """
    connect_to = keeper_host or hosts[0]
    access = _REPLICATED_ACCESS if replicated_access else ""
    keeper_server = ""
    if keeper_host is not None:
        keeper_server = f"""
    <keeper_server>
        <tcp_port>{_KEEPER_PORT}</tcp_port>
        <server_id>1</server_id>
        <log_storage_path>/var/lib/clickhouse/coordination/log</log_storage_path>
        <snapshot_storage_path>/var/lib/clickhouse/coordination/snapshots</snapshot_storage_path>
        <raft_configuration>
            <server><id>1</id><hostname>{keeper_host}</hostname><port>9234</port></server>
        </raft_configuration>
    </keeper_server>"""
    replicas = "".join(f"<replica><host>{host}</host><port>9000</port></replica>" for host in hosts)
    # The digest identity every deploy sets, read from DFE_KEEPER_IDENTITY (keeper_run_args).
    return f"""<clickhouse>{keeper_server}
    <zookeeper>
        <node><host>{connect_to}</host><port>{_KEEPER_PORT}</port></node>
        <identity from_env="DFE_KEEPER_IDENTITY" hide_in_preprocessed="1"/>
    </zookeeper>
    <macros><cluster>{_CLUSTER}</cluster><shard>01</shard><replica>{replica}</replica></macros>
    <remote_servers>
        <{_CLUSTER}><shard><internal_replication>true</internal_replication>{replicas}</shard></{_CLUSTER}>
    </remote_servers>{access}
</clickhouse>
"""


def keeper_identity() -> str:
    """A ``user:password`` Keeper digest identity, generated for one test's servers."""
    return f"dfe:{secrets.token_hex(16)}"


def keeper_run_args(config: str, image: str, *, identity: str) -> list[str]:
    """The tail of a ``docker run`` that writes *config* into the server before it starts.

    Carried in an environment variable rather than a mount, so a remote docker host
    reached over ssh needs no file of its own.

    Args:
        config: The server config, from :func:`keeper_config`.
        image: The ClickHouse image.
        identity: The Keeper digest identity. Replicas sharing a Keeper take the same
            one, as a deployment's do.
    """
    encoded = base64.b64encode(config.encode("utf-8")).decode("ascii")
    script = (
        f'printf %s "$DFE_TEST_CH_CONFIG" | base64 -d > {_KEEPER_CONFIG_PATH} '
        "&& exec /entrypoint.sh"
    )
    return [
        "-e",
        f"DFE_TEST_CH_CONFIG={encoded}",
        "-e",
        f"DFE_KEEPER_IDENTITY={identity}",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        script,
    ]


def _single_node_run_args(image: str) -> list[str]:
    return keeper_run_args(
        keeper_config(replica="r1", keeper_host="localhost", hosts=["localhost"]),
        image,
        identity=keeper_identity(),
    )


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _unavailable(reason: str) -> NoReturn:
    """Skip a test whose backing service could not be had, or fail it in CI.

    A CI run that cannot reach ClickHouse and reports its tests skipped reads as green
    and tested nothing, so there it fails, as the testing standard requires.
    """
    if in_ci():
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


_FORCED_TIERS = {"cluster": "cluster", "1": "cluster", "remote": "remote", "2": "remote"}
_FORCED_TIERS |= {"local": "local", "3": "local"}


def _forced_tier() -> str | None:
    """The tier ``DFE_TEST_TIER`` names, or None for auto."""
    return _FORCED_TIERS.get(os.environ.get("DFE_TEST_TIER", "").strip().lower())


def _cluster_params() -> dict | None:
    """Tier 1: the ``DFE_TEST_CH_*`` target, else the app settings' ClickHouse when asked.

    The app settings read the checkout's .env, which can name a shared ClickHouse no
    test should reach, so they count only under ``DFE_TEST_TIER=cluster``.
    """
    host = os.environ.get("DFE_TEST_CH_HOST")
    if host:
        return {
            "host": host,
            "port": int(os.environ.get("DFE_TEST_CH_PORT", "8123")),
            "username": os.environ.get("DFE_TEST_CH_USER", "default"),
            "password": os.environ.get("DFE_TEST_CH_PASSWORD", ""),
            "secure": False,
        }
    if _forced_tier() != "cluster":
        return None
    try:
        from dfe_engine.settings import load_settings

        s = load_settings().clickhouse
    except Exception:
        return None
    if not s.host or s.host in ("localhost", "127.0.0.1"):
        return None
    return {
        "host": s.host,
        "port": s.port,
        "username": s.username,
        "password": s.password,
        "secure": s.secure,
    }


def _resolve_tier() -> str:
    """Chosen tier: explicit DFE_TEST_TIER, else auto (cluster -> remote -> local)."""
    forced = _forced_tier()
    if forced is not None:
        return forced
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
    except subprocess.SubprocessError, FileNotFoundError:
        return False


def _container_exists(docker: list[str], name: str) -> bool:
    try:
        return bool(_run([*docker, "ps", "-aq", "-f", f"name=^{name}$"], timeout=30).stdout.strip())
    except subprocess.SubprocessError, FileNotFoundError:
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
    _remove_container(docker, name)


def _remove_container(docker: list[str], name: str) -> None:
    """Force-remove ``name`` and its anonymous volumes, best-effort.

    Every image this suite runs declares a VOLUME, so a removal without ``-v`` leaves
    one dangling volume on the docker host per container.
    """
    try:
        _run([*docker, "rm", "-f", "-v", name], timeout=60)
    except subprocess.SubprocessError, FileNotFoundError:
        pass


def _reach_address(spec: str) -> str:
    """Address to reach the published port on (localhost, or the ssh host)."""
    if spec in ("local", "localhost", "127.0.0.1"):
        return "127.0.0.1"
    return spec.removeprefix("ssh://").split("@")[-1]


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    if cmd[0] == "ssh":
        # ssh hands the remote shell one string, so each argument is quoted to arrive whole.
        cmd = [*cmd[:2], shlex.join(cmd[2:])]
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
            except subprocess.SubprocessError, FileNotFoundError:
                time.sleep(0.5)  # someone else is mid-create, or it is going away
                continue
        try:
            _run(run_cmd, timeout=240)  # first image pull can be slow
            return
        except subprocess.SubprocessError, FileNotFoundError:
            time.sleep(0.5)  # lost the create race; loop and join the winner
    _unavailable(f"docker host '{spec}' cannot start or join the shared ClickHouse '{name}'")


def _spin_ch_on_docker(spec: str, *, keep: bool, test: str, keeper: bool = True):
    """Start (or reuse) a CH container on a local/remote docker host.

    Returns ``(params, teardown)``. With keep=True a stable-named container is
    shared, reused across runs and left running; otherwise a container named for
    ``test`` is started and removed on teardown. Skips (never errors) if the
    docker host can't run it. ``keeper=False`` starts the stock image with no
    Keeper, the server the engine refuses.
    """
    docker = _docker_prefix(spec)
    image = os.environ.get("DFE_TEST_CH_IMAGE", _DEFAULT_IMAGE)
    # keep=True shares ONE container, so it takes the suite-scoped name. A
    # throwaway is owned by the test that asked for it: the fixture is
    # function-scoped and the suite runs under xdist, so tests do not share one
    # and a common name would collide rather than pool.
    service = "clickhouse" if keeper else "clickhouse-keeperless"
    name = _container_name(None if keep else test, service)
    run_cmd = [
        *docker,
        "run",
        "-d",
        "--name",
        name,
        *_container_labels(service),
        "-p",
        "0:8123",
        "-e",
        "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
        *(_single_node_run_args(image) if keeper else [image]),
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
            _unavailable(f"docker host '{spec}' cannot start ClickHouse: {exc}")

    def teardown() -> None:
        if keep:
            return  # left running for reuse (DFE_TEST_KEEP)
        _remove_container(docker, name)

    try:
        # docker prints e.g. "0.0.0.0:49153" (take the last line, then the port)
        out = _run([*docker, "port", name, "8123"], timeout=30).stdout.strip()
        port = int(out.splitlines()[-1].rsplit(":", 1)[-1])
    except (subprocess.SubprocessError, ValueError, IndexError) as exc:
        teardown()
        _unavailable(f"could not resolve the ClickHouse port on '{spec}': {exc}")

    params = {
        "host": _reach_address(spec),
        "port": port,
        "username": "default",
        "password": "",
        "secure": False,
    }
    return params, teardown


def _connect_when_ready(
    clickhouse_connect,
    params: dict,
    attempts: int = 40,
    delay: float = 1.0,
    *,
    keeper: bool = True,
):
    """Poll the real readiness signals until both answer, and return the client.

    ``SELECT 1`` says the server is up. A read of Keeper's root says the Replicated
    database can be created, which an embedded Keeper allows a moment later.
    """
    last: Exception | None = None
    for _ in range(attempts):
        try:
            client = clickhouse_connect.get_client(**params)
            client.command("SELECT 1")
            if keeper:
                client.command("SELECT count() FROM system.zookeeper WHERE path = '/'")
            return client
        except Exception as exc:  # CH still starting up
            last = exc
            time.sleep(delay)
    raise last if last is not None else RuntimeError("ClickHouse unreachable")


@pytest.fixture(scope="session")
def local_tier_notice() -> None:
    """Warn once per session when the integration ClickHouse is a container on this host.

    It is the fallback nobody chose, so the run says so. Session scope, so a run of
    hundreds of integration tests carries one notice per worker, not one per test.
    """
    if _resolve_tier() == "local":
        warnings.warn(
            "integration ClickHouse tier: local docker on this host (no cluster in "
            ".env, no DFE_TEST_DOCKER_HOST)",
            RuntimeWarning,
            stacklevel=1,
        )


@pytest.fixture
def ch_params(request, local_tier_notice):
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
            _unavailable(
                "DFE_TEST_TIER=cluster but no CH cluster configured (.env / DFE_TEST_CH_*)"
            )
    else:
        if tier == "remote":
            spec = os.environ.get("DFE_TEST_DOCKER_HOST")
            if not spec:
                _unavailable("DFE_TEST_TIER=remote but DFE_TEST_DOCKER_HOST (user@host) is not set")
        else:
            spec = "local"
        params, teardown = _spin_ch_on_docker(
            spec, keep=_truthy("DFE_TEST_KEEP"), test=request.node.nodeid
        )

    try:
        _connect_when_ready(clickhouse_connect, params).close()  # verify reachable
    except Exception as exc:  # not reachable -> skip locally, fail in CI
        if teardown is not None:
            teardown()
        _unavailable(f"ClickHouse not reachable: {exc}")
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


def _docker_spec(what: str) -> str:
    """The docker host a fixture that starts its own servers runs on: local, or ssh."""
    tier = _resolve_tier()
    if tier == "cluster":
        pytest.skip(f"{what} starts its own servers, which the configured cluster tier cannot")
    if tier == "remote":
        spec = os.environ.get("DFE_TEST_DOCKER_HOST")
        if not spec:
            _unavailable("DFE_TEST_TIER=remote but DFE_TEST_DOCKER_HOST (user@host) is not set")
        return spec
    return "local"


@pytest.fixture
def keeperless_ch_params(request):
    """A stock ClickHouse with no Keeper and no macros, the server the engine refuses."""
    clickhouse_connect = pytest.importorskip("clickhouse_connect")
    spec = _docker_spec("a keeperless server")
    params, teardown = _spin_ch_on_docker(spec, keep=False, test=request.node.nodeid, keeper=False)
    try:
        _connect_when_ready(clickhouse_connect, params, keeper=False).close()
    except Exception as exc:
        teardown()
        _unavailable(f"keeperless ClickHouse not reachable: {exc}")
    try:
        yield params
    finally:
        teardown()


def _published_params(docker: list[str], name: str, spec: str) -> dict:
    """Connection params for a container's published HTTP port."""
    # docker prints e.g. "0.0.0.0:49153" (take the last line, then the port)
    out = _run([*docker, "port", name, "8123"], timeout=30).stdout.strip()
    return {
        "host": _reach_address(spec),
        "port": int(out.splitlines()[-1].rsplit(":", 1)[-1]),
        "username": "default",
        "password": "",
        "secure": False,
    }


@dataclass(frozen=True, slots=True)
class ReplicaPair:
    """Two replicas' connection params, and the docker calls that take replica 2 down and back.

    Attributes:
        first: Replica 1, which runs Keeper.
        second: Replica 2, which connects to replica 1's Keeper.
    """

    first: dict
    second: dict
    docker: tuple[str, ...]
    second_name: str
    spec: str

    def stop_second(self) -> None:
        """Stop replica 2 cleanly, which ends its Keeper session at once."""
        _run([*self.docker, "stop", "-t", "30", self.second_name], timeout=90)

    def start_second(self) -> dict:
        """Start replica 2 again and return its params, whose published port may have moved."""
        import clickhouse_connect

        _run([*self.docker, "start", self.second_name], timeout=90)
        params = _published_params(list(self.docker), self.second_name, self.spec)
        _connect_when_ready(clickhouse_connect, params, attempts=90).close()
        return params


@pytest.fixture
def two_replicas(request):
    """Two ClickHouse replicas of one shard on their own docker network, sharing one Keeper.

    Replica 1 runs Keeper embedded and replica 2 connects to it, and both declare
    the ``{cluster}`` macro over a ``remote_servers`` cluster naming the pair, which
    is the shape a DFE cluster deployment has. SQL users and roles live in Keeper
    unless the test parametrises this fixture indirectly with False. Yields a
    :class:`ReplicaPair`, and removes both containers and the network on teardown.
    """
    clickhouse_connect = pytest.importorskip("clickhouse_connect")
    replicated_access = getattr(request, "param", True)
    spec = _docker_spec("a two-replica cluster")
    docker = _docker_prefix(spec)
    image = os.environ.get("DFE_TEST_CH_IMAGE", _DEFAULT_IMAGE)
    test = request.node.nodeid
    network = _container_name(test, "net")
    aliases = ("chr1", "chr2")
    names = [_container_name(test, f"clickhouse-{alias}") for alias in aliases]

    def teardown() -> None:
        for name in names:
            _remove_container(docker, name)
        try:
            _run([*docker, "network", "rm", network], timeout=60)
        except subprocess.SubprocessError, FileNotFoundError:
            pass

    teardown()
    params: list[dict] = []
    # One identity for both replicas, as a deployment's replicas share one.
    identity = keeper_identity()
    try:
        _run([*docker, "network", "create", "--label", _SUITE_LABEL, network], timeout=60)
        for index, (alias, name) in enumerate(zip(aliases, names, strict=True)):
            config = keeper_config(
                replica=alias,
                keeper_host=aliases[0] if index == 0 else None,
                hosts=list(aliases),
                replicated_access=replicated_access,
            )
            _run(
                [
                    *docker,
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--hostname",
                    alias,
                    "--network",
                    network,
                    "--network-alias",
                    alias,
                    *_container_labels("clickhouse"),
                    "-p",
                    "0:8123",
                    "-e",
                    "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
                    *keeper_run_args(config, image, identity=identity),
                ],
                timeout=240,
            )
            params.append(_published_params(docker, name, spec))
            if index == 0:
                # Replicated access storage refuses to boot without Keeper (exit 231),
                # so replica 2 starts only once replica 1's Keeper answers.
                _connect_when_ready(clickhouse_connect, params[0], attempts=90).close()
        for replica in params:
            _connect_when_ready(clickhouse_connect, replica, attempts=90).close()
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, IndexError) as exc:
        teardown()
        _unavailable(f"docker host '{spec}' cannot start a two-replica ClickHouse: {exc}")
    except Exception as exc:
        teardown()
        _unavailable(f"the two-replica ClickHouse did not come up: {exc}")
    try:
        yield ReplicaPair(
            first=params[0],
            second=params[1],
            docker=tuple(docker),
            second_name=names[1],
            spec=spec,
        )
    finally:
        teardown()


@pytest.fixture
def late_clickhouse(request):
    """A ClickHouse whose address is fixed now and which starts only when asked.

    Yields ``(params, start)``. Nothing listens on ``params`` until the test calls
    ``start()``, which runs the container on that port and returns once it answers,
    so a test can bring up whatever depends on ClickHouse first. Local docker only:
    the port is reserved on this host before the server exists. Removed on teardown.
    """
    import socket

    clickhouse_connect = pytest.importorskip("clickhouse_connect")
    if _resolve_tier() != "local":
        pytest.skip("a ClickHouse that starts late needs the local docker tier")
    docker = _docker_prefix("local")
    name = _container_name(request.node.nodeid, "clickhouse")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    params = {
        "host": "127.0.0.1",
        "port": port,
        "username": "default",
        "password": "",
        "secure": False,
    }
    run_cmd = [
        *docker,
        "run",
        "-d",
        "--name",
        name,
        *_container_labels("clickhouse"),
        "-p",
        f"127.0.0.1:{port}:8123",
        "-e",
        "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
        *_single_node_run_args(os.environ.get("DFE_TEST_CH_IMAGE", _DEFAULT_IMAGE)),
    ]

    def start() -> dict:
        _reap_stale(docker, name)
        try:
            _run(run_cmd, timeout=240)  # first image pull can be slow
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            _unavailable(f"local docker cannot start ClickHouse: {exc}")
        _connect_when_ready(clickhouse_connect, params).close()
        return params

    try:
        yield params, start
    finally:
        _remove_container(docker, name)


@pytest.fixture
def dfe_db(ch_client):
    """An isolated database carrying the REAL landing, detection and coordination tables.

    Built from the same specs the core schema applies, so a hunt writes into the
    detection table a deployment actually has rather than a stand-in shaped to suit
    the test. The runner only asserts its coordination tables exist, so they are
    rendered from the manifest the engine's schema phase applies at boot.
    """
    import uuid

    from dfe_schemas.clickhouse import PlatformProbe

    from dfe_engine.hunt_runner.ch_coordinator import _COORDINATION_IDS
    from dfe_engine.schema.applier import SchemaApplier
    from dfe_engine.schema.ddl_writer import DDLFileWriter
    from dfe_engine.schema.plan import render_one

    db = f"dfe_core_{uuid.uuid4().hex[:8]}"
    platform = PlatformProbe(ch_client).platform
    applier = SchemaApplier(ch_client, platform)
    applier.ensure_database(db)
    writer = DDLFileWriter(platform=platform, database=db)
    for spec in (writer.default_table_spec(), writer.detection_table_spec()):
        applier.ensure_table(db, spec.name, spec.columns, spec.config)
    for object_id in _COORDINATION_IDS:
        for statement in render_one(object_id, data_database=db, platform=platform).statements:
            ch_client.command(statement)
    try:
        yield db
    finally:
        try:
            drop_database_and_policies(ch_client, db)
        except Exception:
            pass


@pytest.fixture
def manager_client(ch_params):
    """The engine's own ClickHouseManager client wrapper, on the harness's ClickHouse.

    Code that calls ``execute`` or goes through the manager's resilience layer needs
    this wrapper; the raw ``ch_client`` is not a substitute.
    """
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

    manager = ClickHouseManager(
        target_config_data={
            "ch_host": ch_params["host"],
            "ch_port": ch_params["port"],
            "ch_username": ch_params["username"],
            "ch_password": ch_params["password"],
            "ch_secure": ch_params["secure"],
        }
    )
    try:
        yield manager.get_clickhouse_client()
    finally:
        manager.close()


@pytest.fixture
def clickhouse_test_database(ch_client):
    """An empty Replicated database on the harness's ClickHouse, dropped after the test.

    Created the way the engine creates its own, because every DFE table DDL is the
    argumentless Replicated form that only a Replicated database accepts.
    """
    import uuid

    from dfe_schemas.clickhouse import PlatformProbe, create_database

    db = f"dfe_test_{uuid.uuid4().hex[:8]}"
    ch_client.command(create_database(db, PlatformProbe(ch_client).platform))
    try:
        yield db
    finally:
        try:
            drop_database_and_policies(ch_client, db)
        except Exception:
            pass


@pytest.fixture
def clickhouse_client(ch_params, clickhouse_test_database):
    """A raw client on the harness's ClickHouse whose default database is the test one."""
    import clickhouse_connect

    client = clickhouse_connect.get_client(**ch_params, database=clickhouse_test_database)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def otel_db(ch_client, clickhouse_test_database):
    """The isolated database carrying the three OTel metrics tables, rendered from the manifest."""
    from dfe_engine.schema.plan import render_one

    db = clickhouse_test_database
    for object_id in (
        "data.otel_metrics_gauge",
        "data.otel_metrics_sum",
        "data.otel_metrics_histogram",
    ):
        for statement in render_one(object_id, data_database=db).statements:
            ch_client.command(statement)
    return db


@pytest.fixture
def stack(
    ch_client: Any, ch_params: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[SimpleNamespace]:
    """The engine API over a real ClickHouse carrying the core tables at a 90-day default.

    Yields ``client`` (a TestClient), ``crud``, ``database`` and the admin's ``headers``. Teardown drops the database and the row policies its deploys left, which outlive it.
    """
    import uuid

    from tests.support.engine_stack import api_settings

    database = f"dfe_engine_api_{uuid.uuid4().hex[:8]}"
    settings = api_settings(ch_params=ch_params, database=database, tmp_path=tmp_path)
    yield from _engine_api(
        ch_client=ch_client,
        database=database,
        monkeypatch=monkeypatch,
        settings=settings,
        tmp_path=tmp_path,
    )


def _kafka_settings(*, ch_params: dict, kafka_bootstrap: str, tmp_path: Path) -> tuple[str, Any]:
    """A fresh database name and the ``stack`` settings with source topics managed on *kafka_bootstrap*."""
    import uuid

    from dfe_engine.settings import KafkaSettings
    from tests.support.engine_stack import api_settings

    database = f"dfe_engine_api_{uuid.uuid4().hex[:8]}"
    settings = api_settings(ch_params=ch_params, database=database, tmp_path=tmp_path)
    settings.kafka = KafkaSettings(
        bootstrap_servers=kafka_bootstrap,
        bootstrap_topics=False,
        ensure_topics=True,
        topic_partitions=2,
        topic_replication_factor=1,
    )
    # Production+team routes the derived app writes to a review PR, and this stack has no forge.
    settings.gitops.mode = "solo"
    return database, settings


@pytest.fixture
def kafka_stack(
    ch_client: Any,
    ch_params: dict,
    kafka_bootstrap: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[SimpleNamespace]:
    """The ``stack`` engine API with its sources' topics managed on a real Kafka broker."""
    database, settings = _kafka_settings(
        ch_params=ch_params, kafka_bootstrap=kafka_bootstrap, tmp_path=tmp_path
    )
    yield from _engine_api(
        ch_client=ch_client,
        database=database,
        monkeypatch=monkeypatch,
        settings=settings,
        tmp_path=tmp_path,
    )


@pytest.fixture
def kafka_engine_restarts(
    ch_client: Any,
    ch_params: dict,
    kafka_bootstrap: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Callable[[], AbstractContextManager[SimpleNamespace]]]:
    """Start the ``kafka_stack`` engine API more than once over one deployment.

    Gitops is on, so every process opens the same deploy repo at startup and reads
    the sources and app instances the last one left. Yields a callable whose
    context is one running process.
    """
    database, settings = _kafka_settings(
        ch_params=ch_params, kafka_bootstrap=kafka_bootstrap, tmp_path=tmp_path
    )
    settings.gitops.enabled = True
    settings.gitops.local_path = str(tmp_path / "deploy")
    settings.gitops.push = False
    _prepare_engine(ch_client=ch_client, monkeypatch=monkeypatch, settings=settings)
    try:
        yield functools.partial(
            _engine_process, database=database, settings=settings, tmp_path=tmp_path
        )
    finally:
        drop_database_and_policies(client=ch_client, database=database)


def _prepare_engine(*, ch_client: Any, monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    """Give the harness's ClickHouse the core tables *settings* name, as the engine expects at start."""
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.schema.phase import apply_plan
    from dfe_engine.schema.plan import build_plan

    # The build and plan routes read the process settings rather than the app's.
    monkeypatch.setattr("dfe_engine.settings._settings", settings)
    ClickHouseManager.reset_instance()
    report = apply_plan(client=ch_client, plan=build_plan(client=ch_client, settings=settings))
    assert not (report.refused), [outcome.describe() for outcome in report.refused]


@contextmanager
def _engine_process(*, database: str, settings: Any, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """One engine API process over *settings*, its admin signed in.

    Without gitops on, the process gets a local deploy repo once it has started.
    """
    from fastapi.testclient import TestClient

    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries, create_access_token
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.gitcrud import GitCrud, default_registry
    from dfe_engine.gitops.repo import GitopsRepo
    from tests.support.engine_stack import ADMIN_PASSWORD

    app = create_app(settings=settings)
    token = create_access_token(
        data={"org_id": "test-org", "roles": ["admin"], "sub": "admin"}, settings=settings
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # Off the issued password, which refuses every other call until changed.
            app.state.account_store.reset_password(new_password=ADMIN_PASSWORD, username="admin")
            crud = app.state.gitcrud
            if crud is None:
                crud = GitCrud(
                    registry=default_registry(),
                    repo=GitopsRepo(local_path=str(tmp_path / "deploy"), push=False),
                )
                app.state.gitcrud = crud
            yield SimpleNamespace(
                client=client,
                crud=crud,
                database=database,
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        _registries.clear()
        ClickHouseManager.reset_instance()


def _engine_api(
    *,
    ch_client: Any,
    database: str,
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
    tmp_path: Path,
) -> Iterator[SimpleNamespace]:
    """The engine API built from *settings*, over a ClickHouse given the core tables first."""
    _prepare_engine(ch_client=ch_client, monkeypatch=monkeypatch, settings=settings)
    try:
        with _engine_process(database=database, settings=settings, tmp_path=tmp_path) as stack:
            yield stack
    finally:
        drop_database_and_policies(client=ch_client, database=database)


def _mongo_when_ready(uri: str, timeout: float = 180.0, delay: float = 1.0) -> None:
    """Poll until an authenticated listDatabases succeeds; ping answers before the backend.

    The eval image initialises its PostgreSQL on first start, and with one container
    per xdist worker starting at once that has taken longer than a minute on CI.
    """
    from pymongo import MongoClient

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        client = MongoClient(uri, serverSelectionTimeoutMS=1000)
        try:
            client.list_database_names()
            return
        except Exception as exc:  # FerretDB or its PostgreSQL still starting
            last = exc
            time.sleep(delay)
        finally:
            client.close()
    raise last if last is not None else RuntimeError("document store unreachable")


def _log_tail(docker: list[str], name: str, lines: int = 15) -> str:
    """The container's last log lines on one line, or why they could not be read."""
    try:
        proc = _run([*docker, "logs", "--tail", str(lines), name], timeout=30)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return f"<unavailable: {exc}>"
    return " | ".join((proc.stdout + proc.stderr).split("\n")).strip(" |")


@pytest.fixture(scope="session")
def mongo_uri():
    """A mongo-wire document store URI: ``DFE_TEST_MONGO_URI``, else FerretDB on docker.

    Session-scoped, so each xdist worker starts one container and its tests share it;
    every test isolates itself in a throwaway database. The name carries the pid
    because concurrent runs on one host each have a ``gw0``.
    """
    configured = os.environ.get("DFE_TEST_MONGO_URI", "").strip()
    if configured:
        yield configured
        return

    spec = os.environ.get("DFE_TEST_DOCKER_HOST") or "local"
    docker = _docker_prefix(spec)
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    name = _container_name(f"{worker}-{os.getpid()}", "ferretdb")
    password = secrets.token_hex(16)
    run_cmd = [
        *docker,
        "run",
        "-d",
        "--name",
        name,
        *_container_labels("ferretdb"),
        "-p",
        "0:27017",
        "-e",
        f"POSTGRES_USER={_FERRETDB_USER}",
        "-e",
        f"POSTGRES_PASSWORD={password}",
        _FERRETDB_IMAGE,
    ]
    try:
        _run(run_cmd, timeout=240)  # first image pull can be slow
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        _unavailable(f"docker host '{spec}' cannot start FerretDB: {exc}")

    def teardown() -> None:
        _remove_container(docker, name)

    try:
        out = _run([*docker, "port", name, "27017"], timeout=30).stdout.strip()
        port = int(out.splitlines()[-1].rsplit(":", 1)[-1])
        uri = f"mongodb://{_FERRETDB_USER}:{password}@{_reach_address(spec)}:{port}/"
        _mongo_when_ready(uri)
    except Exception as exc:
        tail = _log_tail(docker, name).replace(password, "***")
        teardown()
        _unavailable(f"FerretDB on '{spec}' did not come up: {exc}; container log: {tail}")
    try:
        yield uri
    finally:
        teardown()


def _kafka_env(port: int) -> list[str]:
    """The ``-e`` arguments for a one-node KRaft broker that advertises the host port it is published on."""
    env = {
        "KAFKA_NODE_ID": "1",
        "KAFKA_PROCESS_ROLES": "broker,controller",
        "KAFKA_LISTENERS": f"PLAINTEXT://:{port},CONTROLLER://:9093",
        "KAFKA_ADVERTISED_LISTENERS": f"PLAINTEXT://127.0.0.1:{port}",
        "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
        "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
        "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@localhost:9093",
        "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
        "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
        "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
        "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
        # DFE creates every topic it uses, so a produce to a deleted one must not bring it back.
        "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false",
    }
    args: list[str] = []
    for key, value in env.items():
        args.extend(["-e", f"{key}={value}"])
    return args


def _broker_when_ready(bootstrap: str, timeout: float = 90.0, delay: float = 0.5) -> None:
    """Poll until the broker answers metadata and has a group coordinator, raising once *timeout* passes.

    The coordinator lookup makes a fresh broker create its offsets topic, which
    the first group commit otherwise races and loses with NOT_COORDINATOR.
    """
    from confluent_kafka.admin import AdminClient

    admin = AdminClient({"bootstrap.servers": bootstrap})
    probe = "dfe-engine-test-coordinator-probe"
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            admin.list_topics(timeout=2)
            admin.describe_consumer_groups([probe], request_timeout=2)[probe].result()
            return
        except Exception as exc:  # broker or coordinator still starting
            last = exc
            time.sleep(delay)
    raise last if last is not None else RuntimeError("Kafka unreachable")


@pytest.fixture(scope="session")
def kafka_bootstrap() -> Iterator[str]:
    """A real Kafka broker's bootstrap address: ``DFE_TEST_KAFKA_BOOTSTRAP``, else one broker on local docker.

    Session-scoped, so each xdist worker starts one broker and its tests share it,
    each naming its own topics. Local docker only, because the broker advertises
    the port it is published on and only this host can be asked for a free one.
    """
    import socket

    configured = os.environ.get("DFE_TEST_KAFKA_BOOTSTRAP", "").strip()
    if configured:
        yield configured
        return

    docker = _docker_prefix("local")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    name = _container_name(f"{worker}-{os.getpid()}", "kafka")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    run_cmd = [
        *docker,
        "run",
        "-d",
        "--name",
        name,
        *_container_labels("kafka"),
        "-p",
        f"127.0.0.1:{port}:{port}",
        *_kafka_env(port),
        _KAFKA_IMAGE,
    ]

    def teardown() -> None:
        _remove_container(docker, name)

    try:
        _run(run_cmd, timeout=240)  # first image pull can be slow
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        teardown()
        _unavailable(f"local docker cannot start Kafka: {exc}")
    bootstrap = f"127.0.0.1:{port}"
    try:
        _broker_when_ready(bootstrap)
    except Exception as exc:
        tail = _log_tail(docker, name)
        teardown()
        _unavailable(f"Kafka on local docker did not come up: {exc}; container log: {tail}")
    try:
        yield bootstrap
    finally:
        teardown()
