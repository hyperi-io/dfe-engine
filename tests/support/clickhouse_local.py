#  Project:      dfe-engine
#  File:         tests/support/clickhouse_local.py
#  Purpose:      The pinned clickhouse-local the ClickHouse function-drift tests ask
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The clickhouse-local the drift tests compare the caller-SQL guard against.

The guard's lists are checked against the function lists of a real ClickHouse, so a
function a newer server adds fails the build instead of reaching a caller unrefused.
The image is the newest stable release a deployment can run, which is ahead of the
LTS line the integration tests pin: ClickHouse Cloud tracks the stable line.
"""

import functools
import shutil
import subprocess

import pytest

from tests.support.producer_contract import in_ci

# renovate: datasource=docker depName=clickhouse/clickhouse-server
_TAG = "26.10.3.7"
_DIGEST = "sha256:739e099abd1157390960a59e4532d21781d8fde57e6e4151cb920ea02c30b1ce"
IMAGE = f"clickhouse/clickhouse-server:{_TAG}@{_DIGEST}"


@functools.cache
def _docker_usable() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=60,
    )
    return probe.returncode == 0


def clickhouse_local() -> list[str]:
    """The command prefix that runs clickhouse-local at the pinned version.

    Docker runs the pinned image. A clickhouse-local on PATH is the fallback for a
    host without Docker, at whatever version it carries.

    Returns:
        The argv prefix, to which the caller appends ``--query`` and its arguments.
    """
    if _docker_usable():
        return ["docker", "run", "--rm", "-i", "--entrypoint", "clickhouse-local", IMAGE]
    on_path = shutil.which("clickhouse-local")
    if on_path is not None:
        return [on_path]
    reason = f"neither Docker nor clickhouse-local is available to run {IMAGE}"
    if in_ci():
        pytest.fail(reason)
    pytest.skip(reason)


def clickhouse_names(query: str) -> list[str]:
    """Run *query* in clickhouse-local and return the names it prints, one per line."""
    result = subprocess.run(
        [*clickhouse_local(), "--query", query],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.split()
