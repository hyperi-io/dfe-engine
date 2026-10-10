#  Project:      dfe-engine
#  File:         tests/support/deploy_remote.py
#  Purpose:      A bare deploy repo with one commit, for gitops clones to fetch and push
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A bare remote a test owns, standing in for the forge that hosts the deploy repo."""

from pathlib import Path

from dulwich import porcelain


def seeded_remote(tmp_path: Path) -> tuple[Path, str]:
    """Create a bare repo under ``tmp_path`` with one commit; returns it and its branch."""
    remote = tmp_path / "remote.git"
    porcelain.init(str(remote), bare=True)
    seed = tmp_path / "seed"
    porcelain.clone(str(remote), str(seed))
    (seed / "README").write_text("seed\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "README")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), str(remote), f"refs/heads/{branch}".encode())
    return remote, branch
