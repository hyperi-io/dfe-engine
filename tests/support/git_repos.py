#  Project:      dfe-engine
#  File:         tests/support/git_repos.py
#  Purpose:      A real git repository a test owns, for asserting git detection
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A repository built inside ``tmp_path``, so a walk up from below it stops there.

scalo's git detection answers "inside a repository", so a test that asserts on a
directory with no repository of its own reads whatever encloses ``tmp_path`` --
nothing under ``/tmp``, the checkout itself under a redirected ``--basetemp``.
"""

from pathlib import Path

from dulwich import porcelain
from dulwich.refs import Ref
from dulwich.repo import Repo


def own_repo(root: Path, *, branch: str) -> Path:
    """Create a repository at ``root`` with one commit on ``branch``, and return it."""
    root.mkdir(parents=True, exist_ok=True)
    porcelain.init(str(root))
    with Repo(str(root)) as repo:
        repo.refs.set_symbolic_ref(Ref(b"HEAD"), Ref(f"refs/heads/{branch}".encode()))
    seed = root / "README"
    seed.write_text("seed\n", encoding="utf-8")
    porcelain.add(str(root), paths=[str(seed)])
    porcelain.commit(str(root), message=b"seed", author=b"t <t@t>", committer=b"t <t@t>")
    return root
