#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_repo_packing.py
#  Purpose:      A deploy-repo write packs its loose objects without repacking the history
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A write packs the clone's loose objects once ``pack.loose_objects`` have built up.

dulwich's own gc repacks every object the repo holds, so the clone switches it off and
packs only the loose objects, which costs the same at any history length. Real git,
tempdirs, no mocks.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Commit
from dulwich.repo import Repo
from prometheus_client.parser import text_string_to_metric_families
from scalo.logger import logger
from scalo.metrics import create_metrics

from dfe_engine.gitops.metrics import LOOSE_PACKS, GitopsMetrics
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import GitopsPackSettings

# Low enough that a few dozen one-file writes cross it, as thousands do at the default.
_LOOSE_OBJECTS = 20
_WRITES = 40


@pytest.fixture
def captured_logs() -> Iterator[list[str]]:
    """Lines scalo's logger emits inside the test."""
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="DEBUG", format="{level} {message} {extra}")
    try:
        yield lines
    finally:
        logger.remove(sink_id)


def _clone(tmp_path: Path, loose_objects: int, metrics: GitopsMetrics | None = None) -> GitopsRepo:
    repo = GitopsRepo(
        local_path=str(tmp_path / "work"),
        push=False,
        metrics=metrics,
        pack=GitopsPackSettings(loose_objects=loose_objects),
    )
    repo.ensure()
    return repo


def _write_many(repo: GitopsRepo) -> None:
    for i in range(_WRITES):
        result = repo.publish({"config/rules/r.yaml": f"n: {i}\n"}, message=f"rule {i}")
        assert result.changed


def _files(git: Repo, commit_sha: str) -> set[str]:
    """Every path in a commit's tree."""
    commit = git[commit_sha.encode()]
    assert isinstance(commit, Commit)
    return {entry.path.decode() for entry in iter_tree_contents(git.object_store, commit.tree)}


def _sample(manager, name: str, labels: dict[str, str]) -> float:
    """One counter sample, read off the real exposition."""
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


def test_writes_pack_their_loose_objects_without_repacking_the_history(tmp_path: Path) -> None:
    repo = _clone(tmp_path, _LOOSE_OBJECTS)
    assert repo.publish({"seed.yaml": "seed: 1\n"}, message="seed").changed
    # dulwich's gc packs tags into packed-refs, so a tag makes that file its trace.
    porcelain.tag_create(str(repo.path), b"seed")

    _write_many(repo)

    with Repo(str(repo.path)) as git:
        store = git.object_store
        assert store.count_loose_objects() < _LOOSE_OBJECTS
        # dulwich's gc leaves one pack and moves the tag into packed-refs. Packing only
        # the loose objects adds a pack per round and leaves the refs alone.
        assert len(list(store.packs)) >= 2
        assert not (repo.path / ".git" / "packed-refs").exists()
        messages = [entry.commit.message for entry in git.get_walker()]
    assert messages == [f"rule {i}".encode() for i in reversed(range(_WRITES))] + [b"seed"]
    assert (repo.path / "config" / "rules" / "r.yaml").read_text() == f"n: {_WRITES - 1}\n"


def test_loose_objects_of_zero_leaves_them_loose(tmp_path: Path) -> None:
    repo = _clone(tmp_path, 0)

    _write_many(repo)

    with Repo(str(repo.path)) as git:
        assert list(git.object_store.packs) == []
        assert git.object_store.count_loose_objects() >= _WRITES


def test_a_pack_directory_that_refuses_writes_does_not_fail_the_write(
    tmp_path: Path, captured_logs: list[str]
) -> None:
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    # Six loose objects to a pack, so the seeds put the next write past the threshold.
    repo = _clone(tmp_path, 6, GitopsMetrics(manager))
    repo.publish({"seed.yaml": "seed: 1\n"}, message="seed")
    repo.publish({"seed2.yaml": "seed: 2\n"}, message="seed2")
    pack_dir = repo.path / ".git" / "objects" / "pack"
    assert list(pack_dir.iterdir()) == []
    # A pack directory that points nowhere refuses a new pack for root as well; with
    # no packs yet, every object is still loose and readable.
    pack_dir.rmdir()
    pack_dir.symlink_to(tmp_path / "gone")

    try:
        first = repo.publish({"a.yaml": "a: 1\n"}, message="write A")
        second = repo.publish({"b.yaml": "b: 1\n"}, message="write B")
    finally:
        pack_dir.unlink()
        pack_dir.mkdir()

    assert first.commit_sha is not None
    assert second.commit_sha is not None
    with Repo(str(repo.path)) as git:
        messages = [entry.commit.message for entry in git.get_walker()]
        files_a = _files(git, first.commit_sha)
        files_b = _files(git, second.commit_sha)
    assert messages == [b"write B", b"write A", b"seed2", b"seed"]
    assert files_a == {"seed.yaml", "seed2.yaml", "a.yaml"}
    assert files_b == files_a | {"b.yaml"}
    assert _sample(manager, LOOSE_PACKS, {"outcome": "failed"}) == 2
    warnings = [line for line in captured_logs if line.startswith("WARNING ") and "pack" in line]
    assert len(warnings) == 1
