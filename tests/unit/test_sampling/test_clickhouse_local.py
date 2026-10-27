#  Project:      dfe-engine
#  File:         tests/unit/test_sampling/test_clickhouse_local.py
#  Purpose:      The drift tests fail in CI, and skip elsewhere, when no clickhouse-local can run
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A CI run with no way to run clickhouse-local is a red build, never a skipped guard check."""

import pytest
from _pytest.outcomes import Failed, Skipped

from tests.support import clickhouse_local as support


@pytest.mark.parametrize(
    ("docker", "on_path", "ci", "outcome"),
    [
        pytest.param(True, "/usr/bin/clickhouse-local", "", "docker", id="docker_wins_for_the_pin"),
        pytest.param(False, "/usr/bin/clickhouse-local", "", "path", id="path_without_docker"),
        pytest.param(False, None, "", Skipped, id="nothing_locally_skips"),
        pytest.param(False, None, "true", Failed, id="nothing_in_ci_fails"),
        pytest.param(False, None, "1", Failed, id="ci_one_fails"),
    ],
)
def test_the_command_follows_what_the_host_has(monkeypatch, docker, on_path, ci, outcome):
    monkeypatch.setattr(support, "_docker_usable", lambda: docker)
    monkeypatch.setattr(support.shutil, "which", lambda name: on_path)
    monkeypatch.setenv("CI", ci)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    if isinstance(outcome, type):
        with pytest.raises(outcome):
            support.clickhouse_local()
        return
    command = support.clickhouse_local()
    assert (command[0] == "docker") == (outcome == "docker")
    assert (support.IMAGE in command) == (outcome == "docker")
