#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_scalo_logger_dotenv.py
#  Purpose:      init_scalo_logger takes LOG_LEVEL from the .env layer, as the daemon does
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``init_scalo_logger`` resolves its level after the ``.env`` layer has loaded.

Each case runs a fresh interpreter, because scalo's env prefix and the loaded
``.env`` are process-wide. The child gets a minimal environment, so nothing from
the developer's shell reaches it, its output, or a failure message.
"""

import subprocess
import sys
from pathlib import Path

import dfe_engine

# Logged at DEBUG once the logger is up, so it reaches stderr only when DEBUG is the effective level.
_PROBE = "dotenv-level-probe"

_CHILD = f"""
import os
from dfe_engine.api import init_scalo_logger
init_scalo_logger("dfe-test", otel_tracing=False)
from scalo.logger import logger
logger.debug("{_PROBE}")
print(os.environ.get("LOG_LEVEL", ""))
"""


def _run_child(cwd: Path, home: Path, **env: str) -> tuple[int, str, bool]:
    """Run logger init in a fresh interpreter and report what it resolved.

    Returns:
        The exit code, the effective level ``init_logger`` exported, and whether
        the DEBUG probe line was written.
    """
    child_env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(dfe_engine.__file__).parents[1]),
        "OTEL_SDK_DISABLED": "true",
        **env,
    }
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=cwd,
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    lines = proc.stdout.strip().splitlines()
    level = lines[-1] if lines else ""
    emitted = _PROBE in proc.stderr
    return proc.returncode, level, emitted


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    """An empty working directory and an empty home directory."""
    cwd = tmp_path / "work"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    return cwd, home


def test_a_log_level_in_the_working_directory_env_file_sets_the_level(tmp_path):
    cwd, home = _dirs(tmp_path)
    (cwd / ".env").write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

    code, level, emitted = _run_child(cwd, home)

    assert code == 0
    assert level == "DEBUG"
    assert emitted


def test_a_bare_dotenv_cascade_adds_the_home_env_file(tmp_path):
    cwd, home = _dirs(tmp_path)
    (home / ".env").write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

    code, level, emitted = _run_child(cwd, home, DOTENV_CASCADE="true")

    assert code == 0
    assert level == "DEBUG"
    assert emitted


def test_an_exported_log_level_beats_the_env_file(tmp_path):
    cwd, home = _dirs(tmp_path)
    (cwd / ".env").write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

    code, level, emitted = _run_child(cwd, home, LOG_LEVEL="WARNING")

    assert code == 0
    assert level == "WARNING"
    assert not emitted


def test_with_no_env_file_the_level_stays_at_info(tmp_path):
    cwd, home = _dirs(tmp_path)

    code, level, emitted = _run_child(cwd, home)

    assert code == 0
    assert level == "INFO"
    assert not emitted
