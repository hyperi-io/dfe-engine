#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_repo_packing.py
#  Purpose:      A deploy-repo write packs its loose objects without repacking the history
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A write keeps the clone's loose objects below the count where dulwich's own gc runs.

dulwich's gc repacks every object the repo holds, inside the commit, so the write that
crosses ``gc.auto`` pays for the whole history under both locks. Real git, tempdirs,
no mocks.
"""

from pathlib import Path

from dulwich.repo import Repo

from dfe_engine.gitops.repo import GitopsRepo

# Low enough that a few dozen one-file writes cross it, as thousands do at the default.
_GC_AUTO = 40
_WRITES = 40


def _clone(tmp_path: Path, gc_auto: int) -> GitopsRepo:
    repo = GitopsRepo(local_path=str(tmp_path / "work"), push=False)
    repo.ensure()
    with Repo(str(repo.path)) as git:
        config = git.get_config()
        config.set((b"gc",), b"auto", str(gc_auto).encode())
        config.write_to_path()
    return repo


def _write_many(repo: GitopsRepo) -> None:
    for i in range(_WRITES):
        result = repo.publish({"config/rules/r.yaml": f"n: {i}\n"}, message=f"rule {i}")
        assert result.changed


def test_writes_pack_their_loose_objects_without_repacking_the_history(tmp_path: Path) -> None:
    repo = _clone(tmp_path, _GC_AUTO)

    _write_many(repo)

    with Repo(str(repo.path)) as git:
        store = git.object_store
        assert store.count_loose_objects() < _GC_AUTO
        # dulwich's gc leaves one pack and moves the branch into packed-refs; packing
        # only the loose objects adds a pack per round and leaves the refs alone.
        assert len(list(store.packs)) >= 2
        assert not (repo.path / ".git" / "packed-refs").exists()
        messages = [entry.commit.message for entry in git.get_walker()]
    assert messages == [f"rule {i}".encode() for i in reversed(range(_WRITES))]
    assert (repo.path / "config" / "rules" / "r.yaml").read_text() == f"n: {_WRITES - 1}\n"


def test_auto_gc_switched_off_leaves_the_objects_loose(tmp_path: Path) -> None:
    repo = _clone(tmp_path, 0)

    _write_many(repo)

    with Repo(str(repo.path)) as git:
        assert list(git.object_store.packs) == []
        assert git.object_store.count_loose_objects() >= _WRITES
