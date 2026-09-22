#  Project:      dfe-engine
#  File:         tests/e2e/flows/conftest.py
#  Purpose:      The flow suite's fixtures, its parametrisation, and its skip policy
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
from typing import Any

import pytest

from tests.e2e.conftest import E2EConfig
from tests.e2e.engine_api import EngineAPI
from tests.e2e.flows import shapes

# Every skip this suite reports, so pytest_sessionfinish can judge them. Module
# state rather than the config stash: a sub-directory conftest is registered as a
# plugin late, and this has to survive however it was loaded.
_SKIPS: list[tuple[str, str]] = []


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


@pytest.fixture(scope="session")
def deployment(engine: EngineAPI) -> dict[str, Any]:
    """What the deployment says it is, read once for every fact taken off it."""
    return engine.json("GET", "/system/deployment")


@pytest.fixture(scope="session")
def carried(deployment: dict[str, Any]) -> tuple[str, ...]:
    """The transports this deployment carries, read from the deployment itself.

    A profile binds every stage to one at deploy time, and a fixture cannot know
    which profile it is being run against, so this is what decides whether a case
    proves a landing or proves a refusal.
    """
    carries = tuple(deployment["transports"]["available"])
    assert carries, f"the deployment reports no transport at all: {deployment['transports']}"
    return carries


@pytest.fixture(scope="session")
def applies_routing(deployment: dict[str, Any]) -> bool:
    """Whether a deployed source's routing reaches the apps that run it.

    Read for the same reason the transports are: a Compose stack mounts each app's
    config file read-only, so the overlay the engine writes stops at the deploy
    repo and no source created through the API ever reaches the receiver. A
    deployment that does not report the fact is one this suite cannot judge.
    """
    fact = deployment.get("applies_routing")
    assert fact is not None, (
        "the deployment reports no applies_routing, so it runs an engine older "
        "than the fact the routed shapes read"
    )
    return bool(fact)


@pytest.fixture(scope="session")
def offered(engine: EngineAPI) -> tuple[str, ...]:
    """The apps this deployment offers, judged against its own profile by the engine.

    Read rather than derived for the same reason the transports are: a profile
    deploys a subset of the catalogue, a fixture cannot know which one it is
    running against, and a shape whose origin needs an app this tier does not
    deploy is refused at save exactly as a source on the other transport is.
    """
    apps = engine.json("GET", "/apps")
    return tuple(str(a["service"]) for a in apps if a.get("offered", True))


@pytest.fixture(scope="session")
def per_config(engine: EngineAPI) -> tuple[str, ...]:
    """The apps this deployment runs one deployment of PER SOURCE.

    Read off the catalogue for the same reason the offer is: whether a stage
    arrives with its source is the manifest's to say, and a target that cannot
    configure such an instance refuses the source rather than storing one nothing
    runs.
    """
    apps = engine.json("GET", "/apps")
    return tuple(str(a["service"]) for a in apps if a.get("multiplicity") == "per_config")


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
