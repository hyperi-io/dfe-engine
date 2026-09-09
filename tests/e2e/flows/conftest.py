#  Project:      dfe-engine
#  File:         tests/e2e/flows/conftest.py
#  Purpose:      The flow suite's engine client, its parametrisation, and its skip policy
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the flow suite needs before it can assert anything.

The skip policy is why this is a conftest rather than a fixture module: a suite
that reports green with half its cases skipped has proved nothing, so every skip
has to be one a fixture DECLARED. Anything else fails the run, whatever the
individual tests did.

Waiting is the shared ``poll_until`` from the parent conftest - a flow case waits
on Argo's next poll, which is minutes away and not a duration to sleep for.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import E2EConfig
from tests.e2e.flows import shapes

# Every skip this suite reports, so pytest_sessionfinish can judge them. Module
# state rather than the config stash: a sub-directory conftest is registered as a
# plugin late, and this has to survive however it was loaded.
_SKIPS: list[tuple[str, str]] = []


@dataclass
class EngineAPI:
    """The engine's API, with a token this suite keeps fresh.

    A flow run waits on Argo for minutes per case, so it outlives the token it
    started with; every call re-logs in once on a 401 rather than the run dying
    part-way through.
    """

    base: str
    user: str
    password: str
    verify: bool
    token: str | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(verify=self.verify, timeout=120.0)

    def login(self) -> str:
        with self._client() as client:
            response = client.post(
                f"{self.base}/api/v1/auth/login",
                json={"username": self.user, "password": self.password},
            )
        assert response.status_code == 200, f"engine login failed: {response.text}"
        self.token = str(response.json()["access_token"])
        return self.token

    def call(self, method: str, path: str, body: Any = None) -> httpx.Response:
        """One API call, returning the response whatever its status.

        A status is the assertion in several cases here - a refused save is the
        point of the archived shape - so this never raises on one.
        """
        if self.token is None:
            self.login()
        url = f"{self.base}/api/v1{path}"
        response = self._request(method, url, body)
        if response.status_code == 401:
            self.login()
            response = self._request(method, url, body)
        return response

    def _request(self, method: str, url: str, body: Any) -> httpx.Response:
        with self._client() as client:
            return client.request(
                method, url, json=body, headers={"Authorization": f"Bearer {self.token}"}
            )

    def json(self, method: str, path: str, body: Any = None) -> Any:
        """One API call that must succeed, decoded."""
        response = self.call(method, path, body)
        assert response.status_code < 300, (
            f"{method} {path} -> {response.status_code}: {response.text}"
        )
        return response.json() if response.content else None


@pytest.fixture(scope="session")
def engine(e2e: E2EConfig) -> EngineAPI:
    """An authenticated engine API, or the reason the suite cannot run.

    Fails rather than skips: the caller named a transport to prove, so a run that
    cannot reach the engine has not proved it.
    """
    if not e2e.engine_url or not e2e.engine_password:
        pytest.fail(
            "the flow suite writes sources through the engine, so it needs "
            "DFE_E2E_ENGINE_URL and DFE_E2E_ENGINE_PASSWORD"
        )
    api = EngineAPI(
        base=e2e.engine_url.rstrip("/"),
        user=e2e.engine_user,
        password=e2e.engine_password,
        verify=e2e.verify,
    )
    api.login()
    return api


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrise every flow test over shapes x the transports asked for.

    Reading the request here rather than inside a test is what makes ``both`` a
    pair of real cases: each one then FAILS on a deployment that cannot carry it,
    instead of the pair collapsing into one skip.
    """
    if not {"shape", "transport"} <= set(metafunc.fixturenames):
        return
    requested = os.getenv("DFE_E2E_TRANSPORT") or "both"
    metafunc.parametrize(
        ("shape", "transport"),
        [
            pytest.param(shape, transport, id=f"{shape.name}-{transport}")
            for shape in shapes.load_shapes()
            for transport in shapes.transports_for(requested)
        ],
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if report.skipped and isinstance(report.longrepr, tuple):
        _SKIPS.append((report.nodeid, str(report.longrepr[2])))


def undeclared_skips(skips: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The skips no fixture declared - the ones that make a green run a lie.

    Args:
        skips: (node id, reason) for every skip the session reported.

    Returns:
        The subset whose reason does not carry the expected-skip marker.
    """
    return [(node, why) for node, why in skips if shapes.EXPECTED_SKIP not in why]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run on any skip a fixture did not declare.

    A flow case skips for one of two reasons: a gap the fixture NAMES, or
    something that went wrong. The second must not read as a pass, and pytest's
    own exit status cannot tell them apart.
    """
    undeclared = undeclared_skips(_SKIPS)
    if not undeclared:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line(
            f"flow suite: {len(undeclared)} case(s) skipped for a reason no fixture "
            "declared, so this run proved less than it reports:",
            red=True,
        )
        for node, why in undeclared:
            reporter.write_line(f"  {node}: {why}", red=True)
    session.exitstatus = 1
