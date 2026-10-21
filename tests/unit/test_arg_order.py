#  Project:      dfe-engine
#  File:         tests/unit/test_arg_order.py
#  Purpose:      Interleaved file arguments keep their directory's conftest fixtures
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Interleaved file arguments keep their directory's conftest fixtures.

A file in a package, then a file beside the package, then another file in it, is
the shape that loses the package's conftest fixtures on pytest 9.1.1.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tree(root: Path) -> None:
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef shared():\n    return 1\n", encoding="utf-8"
    )
    for name in ("test_first.py", "test_second.py"):
        (pkg / name).write_text("def test_it(shared):\n    assert shared == 1\n", encoding="utf-8")
    (root / "test_beside.py").write_text("def test_it():\n    pass\n", encoding="utf-8")


def test_interleaved_file_arguments_keep_the_package_fixtures(tmp_path):
    _tree(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.support.arg_order",
            "-p",
            "no:cacheprovider",
            "-q",
            "pkg/test_first.py",
            "test_beside.py",
            "pkg/test_second.py",
        ],
        capture_output=True,
        cwd=tmp_path,
        encoding="utf-8",
        env=env,
        errors="replace",
        text=True,
        timeout=120,
        check=False,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert "3 passed" in run.stdout
