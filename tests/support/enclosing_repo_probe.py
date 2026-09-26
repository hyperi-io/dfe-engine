#  Project:      dfe-engine
#  File:         tests/support/enclosing_repo_probe.py
#  Purpose:      A committed registry write under each temp root, for a child pytest run
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A registry write under ``tmp_path`` and under ``tempfile``, run only as a child.

``tests/unit/test_temp_root_isolation.py`` runs this module in a child pytest with
its temp roots placed inside a throwaway repository, then checks that repository's
HEAD. The name keeps it out of normal collection.
"""

import tempfile
from pathlib import Path

from scalo.config import DirectoryConfigStore

from dfe_engine.git_identity import commit_file


def _write(directory: Path) -> None:
    """Write one file into a store rooted at ``directory`` and commit it."""
    store = DirectoryConfigStore(directory=directory, writable=True, refresh_interval=0)
    try:
        target = directory / "probe.yaml"
        target.write_text("name: probe\n", encoding="utf-8")
        commit_file(store, target, "probe: write", author=None)
    finally:
        store.stop()


def test_a_write_under_tmp_path(tmp_path: Path) -> None:
    _write(tmp_path)


def test_a_write_under_tempfile() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _write(Path(directory))
