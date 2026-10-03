#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_loop_stays_free.py
#  Purpose:      A handler waiting on the deploy repo leaves the event loop free
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real app, real deploy repo, and a handler parked at its first deploy-repo touch.

The e2e status route does no I/O, so it answers the moment the event loop is free.
Every case parks one handler and asks it to answer meanwhile: a handler that did its
deploy-repo work on the loop would hold the probe with it. Against a remote forge
that work is a network round trip, which is the wait the UI's reads were queued on.
"""

import ast
import inspect
import textwrap
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from scalo.health import serve_observability
from scalo.metrics import create_metrics

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, get_clickhouse_client
from dfe_engine.api.metrics import WRITES_HELD, ApiMetrics
from dfe_engine.api.v1 import (
    apps,
    auth,
    backing_services,
    gitops,
    governance,
    helm,
    hunts,
    library,
    lifecycle,
    rules,
    sources,
)
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.source import catalogue as source_catalogue_module

from .test_e2e_server import (
    _ADMIN_OWN_PASSWORD,
    _admin,
    _seed,
    _settings,
    _stood_up_as_on_kubernetes,
)

_PROBE = "/api/e2e/status"
_WAIT_SECONDS = 10.0
_PROBE_SECONDS = 5.0
# A write that ran beside the parked one would touch the repo well inside this.
_MUST_NOT_HAPPEN_SECONDS = 0.5

_VRL = "/api/v1/apps/dfe-transform-vrl/seedsource"
_RECEIVER = "/api/v1/apps/dfe-receiver/default"
_CH_REPLICAS = "/api/v1/backing-services/overlays/clickhouse-cluster/vars/clickhouse.replicas"
_NEW_PASSWORD = "loop-probe-Passw0rd-2026"


class _Park:
    """Holds the first call through a patched function until released; later calls pass."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.touched_again = threading.Event()

    def hold(self) -> None:
        """Park the first caller; flag any caller after it."""
        if self.entered.is_set():
            self.touched_again.set()
            return
        self.entered.set()
        assert self.release.wait(timeout=_WAIT_SECONDS * 3)

    @classmethod
    def on_deploy_repo(cls, repo: Any, monkeypatch: pytest.MonkeyPatch) -> _Park:
        """Park the first read or write that reaches the deploy repo."""
        park = cls()
        real_read, real_publish = repo.read_locked, repo.publish

        @contextmanager
        def parked_read():
            park.hold()
            with real_read():
                yield

        def parked_publish(*args, **kwargs):
            park.hold()
            return real_publish(*args, **kwargs)

        monkeypatch.setattr(repo, "read_locked", parked_read)
        monkeypatch.setattr(repo, "publish", parked_publish)
        return park

    @classmethod
    def on_function(cls, owner: Any, name: str, monkeypatch: pytest.MonkeyPatch) -> _Park:
        """Park the first call to ``owner.name``."""
        park = cls()
        real = getattr(owner, name)

        def parked(*args, **kwargs):
            park.hold()
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, parked)
        return park


@pytest.fixture
def seeded(tmp_path):
    """An e2e-server process over a Kubernetes-shaped deploy repo, with every seed applied."""
    _stood_up_as_on_kubernetes(tmp_path / "deploy")
    app = create_app(
        settings=_settings(tmp_path, e2e_server=True, gitops=True, deployment_target="kubernetes")
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for script in (
                "seed_source_with_transform",
                "seed_library_artefact",
                "seed_app_scaling_state",
            ):
                _seed(client, script)
            yield client, _admin(client)
    finally:
        _registries.clear()


def _assert_the_loop_answers(
    client: TestClient,
    park: _Park,
    method: str,
    path: str,
    body: dict | None,
    headers: dict[str, str],
) -> int:
    """Park one request, probe the loop, release; returns the parked request's status."""
    handled: list[int] = []
    answered: list[int] = []

    def call() -> None:
        handled.append(client.request(method, path, json=body, headers=headers).status_code)

    def probe() -> None:
        answered.append(client.get(_PROBE).status_code)

    worker = threading.Thread(target=call, daemon=True)
    prober = threading.Thread(target=probe, daemon=True)
    worker.start()
    try:
        assert park.entered.wait(timeout=_WAIT_SECONDS), f"{method} {path} never reached the park"
        prober.start()
        prober.join(timeout=_PROBE_SECONDS)
        assert answered == [200], f"{method} {path} held the event loop while it waited"
        assert handled == []
    finally:
        park.release.set()
        worker.join(timeout=_WAIT_SECONDS)
        if prober.is_alive():
            prober.join(timeout=_WAIT_SECONDS)
    assert len(handled) == 1
    return handled[0]


_DEPLOY_REPO_CASES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/v1/auth/setup-status", None),
    ("GET", "/api/v1/sources", None),
    ("GET", "/api/v1/sources/seedsource", None),
    ("GET", "/api/v1/sources/seedsource/flow", None),
    ("GET", "/api/v1/sources/seedsource/versions/1.0.0", None),
    ("GET", "/api/v1/apps", None),
    ("GET", _RECEIVER, None),
    ("GET", f"{_RECEIVER}/config", None),
    ("GET", f"{_RECEIVER}/scaling", None),
    ("GET", f"{_RECEIVER}/routing", None),
    ("GET", f"{_RECEIVER}/history", None),
    ("GET", f"{_VRL}/files/transforms", None),
    ("GET", f"{_VRL}/files/transforms/seedsource.vrl", None),
    ("GET", f"{_VRL}/files/transforms/links", None),
    ("GET", "/api/v1/library", None),
    ("GET", "/api/v1/library/seed-artefact", None),
    ("GET", "/api/v1/library/seed-artefact/versions", None),
    ("GET", "/api/v1/library/seed-artefact/versions/1", None),
    ("GET", "/api/v1/library/seed-artefact/usage", None),
    ("GET", "/api/v1/backing-services", None),
    ("GET", "/api/v1/gitops/auto-merge", None),
    ("GET", "/api/v1/gitops/log", None),
    ("GET", "/api/v1/rules", None),
    ("GET", "/api/v1/hunts", None),
    ("GET", "/api/v1/helm/files", None),
    ("GET", "/api/v1/governance/actions", None),
    ("GET", "/api/v1/governance/policies", None),
    ("GET", "/api/v1/lifecycle", None),
    ("PUT", f"{_RECEIVER}/scaling", {"replica_count": 3}),
    ("PUT", f"{_RECEIVER}/config", {"changes": {"extraEnv.LOOP_PROBE": "1"}}),
    ("POST", f"{_RECEIVER}/routing/sync", None),
    ("PUT", f"{_VRL}/files/transforms/probe.vrl", {"content": ".probe = true\n"}),
    ("DELETE", f"{_VRL}/files/transforms/seedsource.vrl", None),
    ("PATCH", "/api/v1/sources/seedsource", {"state": "dormant"}),
    ("POST", "/api/v1/sources/bulk", {"action": "disable", "sources": ["seedsource"]}),
    ("POST", "/api/v1/sources/seedsource/deploy?dry_run=true", None),
    ("DELETE", "/api/v1/sources/seedsource", None),
    ("PUT", "/api/v1/library/seed-artefact/tags/probe", {"version": 2}),
    ("PUT", "/api/v1/library/seed-artefact/state", {"state": "deprecated"}),
    ("PUT", _CH_REPLICAS, {"value": 5}),
    ("PUT", "/api/v1/gitops/auto-merge", {"enabled": True}),
    ("POST", "/api/v1/auth/setup/retire-admin", None),
    (
        "POST",
        "/api/v1/rules/from-hyperdx",
        {
            "raw_sql": "SELECT 1 FROM default.logs WHERE level = 'error'",
            "saved_search_name": "Loop probe",
        },
    ),
    # The break-glass admin is the one account mirrored into the deploy repo.
    ("PUT", "/api/v1/auth/accounts/admin", {"name": "Loop Probe"}),
    ("PUT", "/api/v1/auth/accounts/me", {"name": "Loop Probe"}),
    ("POST", "/api/v1/auth/accounts/admin/reset-password", {"new_password": _NEW_PASSWORD}),
    (
        "POST",
        "/api/v1/auth/accounts/reset-password",
        {"current_password": _ADMIN_OWN_PASSWORD, "new_password": _NEW_PASSWORD},
    ),
    ("GET", "/api/v1/auth/accounts/admin/git-status", None),
]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    _DEPLOY_REPO_CASES,
    ids=[f"{m} {p}" for m, p, _ in _DEPLOY_REPO_CASES],
)
def test_the_loop_answers_while_a_handler_waits_on_the_deploy_repo(
    seeded, monkeypatch, method, path, body
):
    client, headers = seeded
    park = _Park.on_deploy_repo(client.app.state.gitcrud.repo, monkeypatch)

    status = _assert_the_loop_answers(client, park, method, path, body, headers)

    assert status < 500, f"{method} {path} answered {status}"


class _SampledTable:
    """A ClickHouse table carrying ``_org_id``, whose every row read returns one event."""

    def query(self, sql, parameters=None, settings=None):
        if sql.startswith("DESCRIBE TABLE"):
            return SimpleNamespace(result_rows=[["_org_id", "String"], ["_json", "String"]])
        return SimpleNamespace(result_rows=[['{"message": "loop probe"}']])


def test_the_loop_answers_while_a_dry_run_waits_on_the_deploy_repo(seeded, monkeypatch):
    # The dry run samples ClickHouse once the deploy repo answers, and this harness runs none.
    client, headers = seeded
    monkeypatch.setitem(client.app.dependency_overrides, get_clickhouse_client, _SampledTable)
    park = _Park.on_deploy_repo(client.app.state.gitcrud.repo, monkeypatch)
    path = f"{_VRL}/files/transforms/dry-run"

    status = _assert_the_loop_answers(
        client, park, "POST", path, {"name": "seedsource.vrl"}, headers
    )

    assert status == 200, f"POST {path} answered {status}"


_PASSWORD_CHECK_CASES: list[tuple[str, str, dict]] = [
    ("POST", "/api/v1/auth/login", {"username": "admin", "password": "not-the-password"}),
    (
        "POST",
        "/api/v1/auth/accounts/reset-password",
        {"current_password": "not-the-password", "new_password": _NEW_PASSWORD},
    ),
]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    _PASSWORD_CHECK_CASES,
    ids=[f"{m} {p}" for m, p, _ in _PASSWORD_CHECK_CASES],
)
def test_the_loop_answers_while_a_password_is_checked(seeded, monkeypatch, method, path, body):
    """A bcrypt check is a quarter of a second of CPU; on the loop it stalls every caller."""
    client, headers = seeded
    park = _Park.on_function(client.app.state.account_store, "verify_password", monkeypatch)

    status = _assert_the_loop_answers(client, park, method, path, body, headers)

    assert status in (401, 403), status


def test_the_loop_answers_while_the_source_catalogue_is_read(seeded, monkeypatch):
    """Polled by the Sources page; it reads a mounted file, not the deploy repo."""
    client, headers = seeded
    park = _Park.on_function(source_catalogue_module, "source_catalogue", monkeypatch)

    status = _assert_the_loop_answers(
        client, park, "GET", "/api/v1/sources/catalogue", None, headers
    )

    assert status == 200


class TestWritesTakeTurns:
    """Off the loop, two writes could interleave their commits and config renders."""

    def test_a_write_waits_for_the_one_in_flight_and_a_read_does_not(self, seeded, monkeypatch):
        client, headers = seeded
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        client.app.state.api_metrics = ApiMetrics(manager)
        park = _Park.on_deploy_repo(client.app.state.gitcrud.repo, monkeypatch)
        results: dict[str, int] = {}

        def send(key: str, method: str, path: str, body: dict | None) -> None:
            results[key] = client.request(method, path, json=body, headers=headers).status_code

        first = threading.Thread(
            target=send,
            args=("first", "PUT", "/api/v1/gitops/auto-merge", {"enabled": True}),
            daemon=True,
        )
        second = threading.Thread(
            target=send,
            args=("second", "PUT", _CH_REPLICAS, {"value": 5}),
            daemon=True,
        )
        first.start()
        try:
            assert park.entered.wait(timeout=_WAIT_SECONDS)
            second.start()
            # The second write is queued for its turn, so it never reaches the repo.
            assert park.touched_again.wait(timeout=_MUST_NOT_HAPPEN_SECONDS) is False
            # A read is not a write: it goes straight through while the first is parked.
            assert client.get("/api/v1/apps", headers=headers).status_code == 200
        finally:
            park.release.set()
            first.join(timeout=_WAIT_SECONDS)
            second.join(timeout=_WAIT_SECONDS)

        assert results == {"first": 200, "second": 200}
        assert _sample(manager.metrics_text, WRITES_HELD, {"method": "PUT"}) == 1


def _sample(exposition: str, name: str, labels: dict[str, str]) -> float | None:
    """One sample's value from a Prometheus text exposition, or None when absent."""
    for family in text_string_to_metric_families(exposition):
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return None


def test_the_engine_metrics_endpoint_serves_the_new_counters(tmp_path):
    """One manager: the one the daemon's observability server serves on /metrics."""
    _stood_up_as_on_kubernetes(tmp_path / "deploy")
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    app = create_app(
        settings=_settings(tmp_path, e2e_server=True, gitops=True, deployment_target="kubernetes"),
        metrics_manager=manager,
    )
    server = serve_observability(metrics=manager, addr="127.0.0.1:0")
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            _seed(client, "seed_app_scaling_state")
            _seed(client, "reset_all")
        host, port = server.bound_address or ("", 0)
        body = httpx.get(f"http://{host}:{port}/metrics", timeout=_WAIT_SECONDS).text
    finally:
        server.stop()
        _registries.clear()

    assert _sample(body, "gitops_batches_total", {"outcome": "committed"}) == 2
    restored = {"service": "dfe-receiver", "action": "restored"}
    assert _sample(body, "e2e_pool_resets_total", restored) == 1
    families = {family.name for family in text_string_to_metric_families(body)}
    assert "api_writes_held" in families


_THREADED_ROUTERS = [
    sources.router,
    apps.router,
    library.router,
    backing_services.router,
    gitops.router,
    rules.router,
    hunts.router,
    helm.router,
    governance.router,
    lifecycle.router,
]


def _awaits(endpoint: Any) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    return any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in ast.walk(tree))


def _routes(router: Any) -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


@pytest.mark.parametrize("router", _THREADED_ROUTERS, ids=lambda r: r.prefix)
def test_no_handler_on_a_deploy_repo_router_runs_its_body_on_the_loop(router):
    """An async handler with nothing to await is sync work the event loop has to wait out."""
    on_the_loop = [
        route.endpoint.__name__
        for route in _routes(router)
        if inspect.iscoroutinefunction(route.endpoint) and not _awaits(route.endpoint)
    ]

    assert on_the_loop == []


@pytest.mark.parametrize("router", _THREADED_ROUTERS, ids=lambda r: r.prefix)
def test_a_deploy_repo_router_puts_its_writes_in_turn(router):
    assert WRITE_TURN in router.dependencies


def test_the_setup_routes_run_off_the_loop():
    by_name = {route.endpoint.__name__: route for route in _routes(auth.router)}

    for name in ("get_setup_status", "retire_bootstrap_admin"):
        assert not inspect.iscoroutinefunction(by_name[name].endpoint), name
    assert WRITE_TURN in by_name["retire_bootstrap_admin"].dependencies
