"""No index entry may be a gitlink (mode 160000).

The ``schemas`` submodule was replaced by the version-pinned ``dfe-schemas``
wheel, so a gitlink in this repo is a leftover, not a dependency. With no
``.gitmodules`` describing it, ``actions/checkout``'s post-job credential
cleanup walks the entry and fails, which killed the release publish twice
(dfe-engine #367). Git cannot refuse the mode, so this test does.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def test_no_index_entry_is_a_gitlink():
    git = shutil.which("git")
    if git is None or not (REPO / ".git").exists():
        pytest.skip("not a git checkout")

    listing = subprocess.run(
        [git, "-C", str(REPO), "ls-files", "--stage"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    gitlinks = [
        line.split("\t", 1)[1] for line in listing.splitlines() if line.startswith("160000 ")
    ]

    assert not gitlinks, (
        f"gitlink entries in the index (mode 160000): {gitlinks} -- "
        "remove with git rm --cached <path>; the release checkout cannot survive one"
    )
