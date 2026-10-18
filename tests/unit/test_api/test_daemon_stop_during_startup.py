#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_daemon_stop_during_startup.py
#  Purpose:      dfe-engine run stops on SIGTERM or SIGINT while startup waits on ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A real ``dfe-engine run`` child whose ClickHouse is a port nothing listens on.

The schema phase waits ``clickhouse.bootstrap_wait_seconds`` (180 s by default) for
ClickHouse, each attempt on the 60 s resilience budget, so a stop that waits for
startup to return takes minutes.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# The first refused connect, logged from inside the startup ClickHouse wait.
_IN_THE_WAIT = "ClickHouse unavailable - backing off to retry"
# A child that has not reached the wait by now has failed for some other reason.
_REACH_SECONDS = 60.0
# The wait it would otherwise sit out is minutes long.
_STOP_SECONDS = 10.0

_DAEMON = "from dfe_engine.api import run_dev_server; run_dev_server()"


def _unused_port() -> int:
    """A loopback port nothing listens on once this returns."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _daemon_env(tmp_path: Path) -> dict[str, str]:
    """The suite's environment, pointed at a dead ClickHouse and this test's own paths."""
    env = dict(os.environ)
    # No env file and no ~/.env reach the child, so a developer's stack stays out of it.
    for name in ("DFE_ENV_FILE", "DOTENV_CASCADE", "PYTEST_ADDOPTS"):
        env.pop(name, None)
    env.update(
        {
            "DFE_ENV": "test",
            "DFE_CONFIG_DIR": str(tmp_path / "config"),
            "DFE_STORAGE_PATH": str(tmp_path / "artifacts"),
            "DFE_SECRETS_PATH": str(tmp_path / "secrets"),
            "DFE_GITOPS_ENABLED": "false",
            "DFE_CLICKHOUSE_HOST": "127.0.0.1",
            "DFE_CLICKHOUSE_PORT": str(_unused_port()),
            "DFE_CLICKHOUSE_SECURE": "false",
            "DFE_CLICKHOUSE_BOOTSTRAP_TABLES": "true",
            "METRICS_ADDR": "127.0.0.1:0",
        }
    )
    return env


def _read(log: Path) -> str:
    return log.read_text(encoding="utf-8", errors="replace")


def _wait_for(log: Path, text: str, child: subprocess.Popen) -> None:
    """Return once the child has logged ``text``, or fail with what it did log."""
    deadline = time.monotonic() + _REACH_SECONDS
    while time.monotonic() < deadline and child.poll() is None:
        if text in _read(log):
            return
        time.sleep(0.2)
    pytest.fail(f"the daemon never logged {text!r}:\n{_read(log)[-4000:]}")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_stop_during_the_clickhouse_wait_ends_the_daemon_at_once(tmp_path, sig):
    log = tmp_path / "daemon.log"
    with log.open("w", encoding="utf-8") as out:
        child = subprocess.Popen(
            [sys.executable, "-c", _DAEMON, "run"],
            cwd=tmp_path,
            env=_daemon_env(tmp_path),
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for(log, _IN_THE_WAIT, child)
        sent = time.monotonic()
        child.send_signal(sig)
        try:
            child.wait(timeout=_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"still running {_STOP_SECONDS:.0f}s after {sig.name}:\n{_read(log)[-4000:]}"
            )
        elapsed = time.monotonic() - sent
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    # The status a shell gives a process the signal ended.
    assert child.returncode == 128 + sig, _read(log)[-4000:]
    assert elapsed < _STOP_SECONDS
    assert "Traceback" not in _read(log)
