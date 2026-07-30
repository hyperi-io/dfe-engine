#  Project:      dfe-engine
#  File:         tests/integration/test_container_hygiene.py
#  Purpose:      Pin the test-container naming and cleanup convention
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The naming and cleanup convention for containers this suite starts.

Several runs share a developer machine -- this suite alone runs under xdist -n 4 --
so a container has to say what it is, which suite started it, and whose run owns
it. Otherwise ``docker ps`` is a wall of random hex and nobody can tell what is
safe to remove. These tests pin the scheme so it cannot drift back.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests.integration.conftest import (
    _container_labels,
    _container_name,
    _reap_stale,
    _resolve_tier,
)


def test_per_test_container_name_includes_the_test():
    """A throwaway carries the owning test between the suite and the service."""
    assert (
        _container_name("test_hunt_checkpoint", "clickhouse")
        == "dfe-engine-test-integration-test-hunt-checkpoint-clickhouse"
    )


def test_shared_container_name_omits_the_test():
    """The DFE_TEST_KEEP instance is shared, so it takes the suite-scoped name."""
    assert _container_name(None, "clickhouse") == "dfe-engine-test-integration-clickhouse"


def test_two_tests_wanting_one_service_do_not_collide():
    """Distinct tests must get distinct names.

    ``ch_params`` is function-scoped and the suite runs under xdist, so tests do
    not share a container. On one common name the first ``docker run`` wins and
    the rest fail with "name is already in use" -- which is why the throwaway path
    is named per test.
    """
    assert _container_name("test_a", "clickhouse") != _container_name("test_b", "clickhouse")


def test_container_name_is_a_legal_docker_name():
    """Docker only accepts ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``.

    A pytest node name carries brackets and colons once a test is parametrised,
    which docker rejects at create time as what looks like a docker fault. So the
    name is normalised here instead.
    """
    name = _container_name("test_rbac[tenant-a:read]", "ClickHouse")
    assert name == "dfe-engine-test-integration-test-rbac-tenant-a-read--clickhouse"

    def legal(value: str) -> bool:
        head, tail = value[0], value[1:]
        return head.isalnum() and all(c.isalnum() or c in "_.-" for c in tail)

    assert legal(name), f"{name} is not a legal docker container name"
    assert legal(_container_name(None, "clickhouse"))


def test_names_share_one_greppable_prefix():
    """One `docker ps` filter finds everything this repo's suite started."""
    for name in (
        _container_name(None, "clickhouse"),
        _container_name("some_test", "clickhouse"),
    ):
        assert name.startswith("dfe-engine-test-integration-"), f"{name} lacks the suite prefix"


def test_labels_identify_the_suite_and_the_owning_process():
    """The label is what makes a bulk sweep possible when names are unknown.

    ``owner-pid`` has to be THIS process, or it cannot answer whose run left a
    container behind.
    """
    labels = _container_labels("clickhouse")
    assert labels.count("--label") == 4
    values = [v for v in labels if v != "--label"]
    assert "io.hyperi.test.suite=dfe-engine-integration" in values
    assert "io.hyperi.test.repo=dfe-engine" in values
    assert "io.hyperi.test.service=clickhouse" in values
    assert f"io.hyperi.test.owner-pid={os.getpid()}" in values


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            check=False,
            timeout=30,
        ).returncode
        == 0
    )


def test_reap_stale_removes_a_dead_container_but_not_a_live_one():
    """The two halves of the reaper, against real containers.

    A leaked container from a killed run holds its name and would block every
    later run with "name is already in use", so a DEAD one must go. A RUNNING one
    must not: two concurrent runs share the DFE_TEST_KEEP name, and force-removing
    a live one would sabotage a run that did nothing wrong and surface over there
    as a baffling mid-test failure.

    Uses busybox rather than ClickHouse -- this is about the reaper, not the store,
    and a 4MB image keeps it quick.
    """
    if not _docker_available():
        pytest.skip("no docker daemon")

    name = _container_name("test_reap_stale_probe", "busybox")
    docker = ["docker"]

    def exists() -> bool:
        out = subprocess.run(
            ["docker", "ps", "-aq", "-f", f"name=^{name}$"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return bool(out.stdout.strip())

    def running() -> bool:
        out = subprocess.run(
            ["docker", "ps", "-q", "-f", f"name=^{name}$"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return bool(out.stdout.strip())

    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=60)
    try:
        # A LIVE container must survive the reaper.
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                *_container_labels("busybox"),
                "busybox:stable",
                "sleep",
                "120",
            ],
            capture_output=True,
            check=True,
            timeout=120,
        )
        assert running(), "the probe container should be up"
        _reap_stale(docker, name)
        assert running(), "reap_stale must NOT remove a running container"

        # Stopped, it is a leak from someone's killed run, and must go.
        subprocess.run(["docker", "stop", name], capture_output=True, check=True, timeout=60)
        assert exists(), "the probe container should still be present after stop"
        assert not running(), "the probe container should not be running after stop"
        _reap_stale(docker, name)
        assert not exists(), "reap_stale must remove a dead container holding the name"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=60)


def test_a_started_container_carries_the_name_and_labels(ch_params, request):
    """A REAL container from the fixture matches the convention.

    The tests above only exercise the naming helpers, which says nothing about
    whether the fixture calls them -- a convention that is never wired up is
    decoration. So this asks docker what actually got created.

    Skips when the fixture resolved to a cluster or a reused DFE_TEST_KEEP
    container, since neither was started by this test. Note the tier is
    auto-resolved: a non-localhost ClickHouse in `.env` puts a developer on the
    cluster tier, where nothing here starts a container -- so the skip names the
    tier rather than leaving it to be guessed. Force the container path with
    `DFE_TEST_TIER=local`.
    """
    if not _docker_available():
        pytest.skip("no docker daemon")
    tier = _resolve_tier()
    if tier == "cluster":
        pytest.skip("resolved tier is 'cluster', so no container was started")

    expected = _container_name(request.node.name, "clickhouse")

    def inspect(fmt: str) -> str | None:
        out = subprocess.run(
            ["docker", "inspect", expected, "--format", fmt],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return out.stdout.strip() if out.returncode == 0 else None

    name = inspect("{{.Name}}")
    if name is None:
        pytest.skip(
            f"{expected} was not started on this host (tier '{tier}': a reused "
            "DFE_TEST_KEEP container, or a remote docker host)"
        )

    assert name.lstrip("/") == expected, "the container is not named per the convention"
    assert inspect('{{index .Config.Labels "io.hyperi.test.suite"}}') == "dfe-engine-integration"
    assert inspect('{{index .Config.Labels "io.hyperi.test.owner-pid"}}') == str(os.getpid()), (
        "owner-pid must name THIS test process, or it cannot answer whose run left it"
    )
    # ch_params is the fixture under test; touching it keeps the dependency honest.
    assert ch_params["port"] > 0
