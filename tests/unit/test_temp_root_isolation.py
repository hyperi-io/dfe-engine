#  Project:      dfe-engine
#  File:         tests/unit/test_temp_root_isolation.py
#  Purpose:      A test run never commits onto a checkout its temp roots sit inside
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A run whose temp roots sit inside a git checkout leaves that checkout's branch alone.

scalo's DirectoryConfigStore finds its repository by walking up from the store
directory, so a registry write under a temp root inside a checkout commits onto the
branch that checkout has out. Each case runs a real registry write in a child pytest
and reads the checkout's HEAD afterwards.
"""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from dulwich.repo import Repo

from tests.support.git_repos import own_repo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE = "tests/support/enclosing_repo_probe.py"

type Placement = Callable[[Path, Path], tuple[Path, Path]]


def _head(repo_root: Path) -> bytes:
    with Repo(str(repo_root)) as repo:
        return repo.head()


def _run_probe(*, basetemp: Path, tmpdir: Path) -> subprocess.CompletedProcess[str]:
    """Run the probe module in a child pytest with the given temp roots."""
    tmpdir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TMPDIR": str(tmpdir)}
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            PROBE,
            "--basetemp",
            str(basetemp),
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-q",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _basetemp_inside(checkout: Path, outside: Path) -> tuple[Path, Path]:
    return checkout / "bt", outside / "tmp"


def _basetemp_through_a_symlink(checkout: Path, outside: Path) -> tuple[Path, Path]:
    link = outside / "link"
    link.symlink_to(checkout, target_is_directory=True)
    return link / "bt", outside / "tmp"


def _tmpdir_inside(checkout: Path, outside: Path) -> tuple[Path, Path]:
    return outside / "bt", checkout / "tmp"


PLACEMENTS: dict[str, Placement] = {
    "basetemp-inside": _basetemp_inside,
    "basetemp-through-a-symlink": _basetemp_through_a_symlink,
    "tmpdir-inside": _tmpdir_inside,
}


@pytest.mark.parametrize("place", list(PLACEMENTS.values()), ids=list(PLACEMENTS))
def test_a_temp_root_inside_a_checkout_leaves_its_branch_where_it_was(
    tmp_path: Path, place: Placement
):
    checkout = own_repo(tmp_path / "checkout", branch="feature")
    before = _head(checkout)
    outside = tmp_path / "outside"
    outside.mkdir()
    basetemp, tmpdir = place(checkout, outside)

    result = _run_probe(basetemp=basetemp, tmpdir=tmpdir)
    output = result.stdout + result.stderr

    assert _head(checkout) == before, output[-4000:]
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, output[-4000:]
    assert f"inside the git repository at {checkout.resolve()}" in output


def test_temp_roots_outside_any_checkout_run_the_writes(tmp_path: Path):
    result = _run_probe(basetemp=tmp_path / "bt", tmpdir=tmp_path / "tmp")
    output = result.stdout + result.stderr

    assert result.returncode == pytest.ExitCode.OK, output[-4000:]
    assert "2 passed" in result.stdout
