#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_repo_consolidation.py
#  Purpose:      dulwich's gc stays out of every write, read and push
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dulwich's gc never runs inside a write, a read's refresh or a push.

That gc rewrites every object the repo holds, so whichever call reached dulwich's
pack limit paid for the whole history under the clone's locks, and each fetch that
brings objects adds a pack. The clone switches it off and consolidates its packs from
a background task instead. Real git and tempdirs. dulwich's gc is wrapped only to
record that it ran, and still runs.
"""

import asyncio
import os
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from dulwich import gc as dulwich_gc
from dulwich.repo import Repo
from prometheus_client.parser import text_string_to_metric_families
from scalo.logger import logger
from scalo.metrics import create_metrics

from dfe_engine.gitops.consolidation import consolidate_forever
from dfe_engine.gitops.metrics import PACK_CONSOLIDATIONS, GitopsMetrics
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import GitopsPackSettings
from tests.support.deploy_remote import seeded_remote

# A pack limit a dozen writes across two clones goes well past.
_PACKS = 5


@pytest.fixture
def captured_logs() -> Iterator[list[str]]:
    """Lines scalo's logger emits inside the test."""
    lines: list[str] = []
    sink_id = logger.add(lines.append, level="DEBUG", format="{level} {message} {extra}")
    try:
        yield lines
    finally:
        logger.remove(sink_id)


@pytest.fixture
def gc_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The repo path of every run of dulwich's gc, recorded before the run goes ahead."""
    runs: list[str] = []
    real: Callable = dulwich_gc.garbage_collect

    def recording(repo, *args, **kwargs):
        runs.append(str(repo.path))
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(dulwich_gc, "garbage_collect", recording)
    return runs


def _packs(repo: GitopsRepo) -> int:
    with Repo(str(repo.path)) as git:
        return git.object_store.count_pack_files()


def _gc_config(repo: GitopsRepo) -> tuple[bytes, bytes]:
    with Repo(str(repo.path)) as git:
        config = git.get_config()
        return config.get((b"gc",), b"auto"), config.get((b"gc",), b"autoPackLimit")


def _sample(manager, name: str, labels: dict[str, str]) -> float:
    """One counter sample, read off the real exposition."""
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


def _packed_clone(tmp_path: Path, writes: int, metrics: GitopsMetrics | None = None) -> GitopsRepo:
    """A local clone whose every write after the first adds one pack."""
    repo = GitopsRepo(
        local_path=str(tmp_path / "work"),
        push=False,
        metrics=metrics,
        pack=GitopsPackSettings(loose_objects=3, packs=_PACKS),
    )
    repo.ensure()
    for i in range(writes):
        assert repo.publish({"r.yaml": f"n: {i}\n"}, message=f"rule {i}").changed
    return repo


def test_ensure_switches_dulwichs_own_gc_off(tmp_path: Path) -> None:
    remote, branch = seeded_remote(tmp_path)
    cloned = GitopsRepo(
        local_path=str(tmp_path / "a"), repo_url=remote.as_uri(), branch=branch, push=True
    )
    initialised = GitopsRepo(local_path=str(tmp_path / "b"), push=False)
    cloned.ensure()
    initialised.ensure()
    with Repo(str(initialised.path)) as git:
        config = git.get_config()
        config.set((b"gc",), b"auto", b"12")
        config.write_to_path()
    initialised.ensure()

    assert _gc_config(cloned) == (b"0", b"0")
    assert _gc_config(initialised) == (b"0", b"0")


def _clone_with_gc_on(tmp_path: Path) -> GitopsRepo:
    """A reused clone whose config still lets dulwich's own gc run."""
    repo = GitopsRepo(local_path=str(tmp_path / "work"), push=False)
    repo.ensure()
    with Repo(str(repo.path)) as git:
        config = git.get_config()
        config.set((b"gc",), b"auto", b"12")
        config.write_to_path()
    return repo


def _gc_warnings(captured_logs: list[str]) -> list[str]:
    return [line for line in captured_logs if line.startswith("WARNING ") and "dulwich" in line]


def test_a_stale_config_lock_leaves_dulwichs_gc_on_and_the_clone_usable(
    tmp_path: Path, captured_logs: list[str]
) -> None:
    repo = _clone_with_gc_on(tmp_path)
    # What a git process that died mid-write leaves behind.
    lock = repo.path / ".git" / "config.lock"
    lock.write_text("", encoding="utf-8")

    try:
        assert repo.ensure() == repo.path
        assert repo.ensure() == repo.path
    finally:
        lock.unlink()

    assert _gc_config(repo) == (b"12", b"0")
    assert len(_gc_warnings(captured_logs)) == 1
    assert repo.publish({"a.yaml": "a: 1\n"}, message="write A").changed


def test_a_read_only_git_directory_leaves_dulwichs_gc_on_and_the_clone_usable(
    tmp_path: Path, captured_logs: list[str]
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes into a read-only directory")
    repo = _clone_with_gc_on(tmp_path)
    git_dir = repo.path / ".git"
    git_dir.chmod(0o555)

    try:
        assert repo.ensure() == repo.path
    finally:
        git_dir.chmod(0o755)

    assert _gc_config(repo) == (b"12", b"0")
    assert len(_gc_warnings(captured_logs)) == 1


def test_no_write_refresh_or_push_runs_dulwichs_gc(tmp_path: Path, gc_runs: list[str]) -> None:
    remote, branch = seeded_remote(tmp_path)
    pack = GitopsPackSettings(packs=_PACKS)
    first = GitopsRepo(
        local_path=str(tmp_path / "a"),
        repo_url=remote.as_uri(),
        branch=branch,
        push=True,
        pack=pack,
    )
    second = GitopsRepo(
        local_path=str(tmp_path / "b"),
        repo_url=remote.as_uri(),
        branch=branch,
        push=True,
        pack=pack,
    )
    first.ensure()
    second.ensure()

    most = 0
    for i in range(12):
        assert first.publish({f"a{i}.yaml": f"a: {i}\n"}, message=f"from a {i}").pushed
        assert second.refresh()
        assert second.publish({f"b{i}.yaml": f"b: {i}\n"}, message=f"from b {i}").pushed
        assert first.refresh()
        most = max(most, _packs(first), _packs(second))

    assert most > _PACKS
    assert gc_runs == []
    with Repo(str(remote)) as git:
        assert len(list(git.get_walker())) == 25


def test_consolidate_if_due_consolidates_once_the_packs_are_due(
    tmp_path: Path, gc_runs: list[str]
) -> None:
    repo = _packed_clone(tmp_path, writes=8)
    assert _packs(repo) >= _PACKS
    # The clone's config tells dulwich's own gc never to run; consolidation runs anyway.
    assert _gc_config(repo) == (b"0", b"0")

    assert repo.consolidate_if_due() is True

    assert _packs(repo) == 1
    assert gc_runs == [str(repo.path)]
    with Repo(str(repo.path)) as git:
        assert git.object_store.count_loose_objects() == 0
        messages = [entry.commit.message for entry in git.get_walker()]
    assert messages == [f"rule {i}".encode() for i in reversed(range(8))]
    assert (repo.path / "r.yaml").read_text() == "n: 7\n"


def test_consolidate_if_due_leaves_the_packs_alone_below_the_threshold(
    tmp_path: Path, gc_runs: list[str]
) -> None:
    repo = _packed_clone(tmp_path, writes=3)
    before = _packs(repo)
    assert 0 < before < _PACKS

    assert repo.consolidate_if_due() is False

    assert _packs(repo) == before
    assert gc_runs == []


def test_a_corrupt_object_is_an_error_and_the_packs_stay(
    tmp_path: Path, captured_logs: list[str]
) -> None:
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    repo = _packed_clone(tmp_path, writes=8, metrics=GitopsMetrics(manager))
    # A loose object whose header names a type git does not have.
    corrupt = repo.path / ".git" / "objects" / "ff" / ("0" * 38)
    corrupt.parent.mkdir(exist_ok=True)
    corrupt.write_bytes(zlib.compress(b"bogus 5\0hello"))
    before = _packs(repo)
    assert before >= _PACKS

    assert repo.consolidate_if_due() is False

    assert _packs(repo) == before
    assert _sample(manager, PACK_CONSOLIDATIONS, {"outcome": "corrupt"}) == 1
    assert _sample(manager, PACK_CONSOLIDATIONS, {"outcome": "failed"}) == 0
    errors = [line for line in captured_logs if line.startswith("ERROR ") and "corrupt" in line]
    assert len(errors) == 1


def test_a_pack_limit_of_zero_never_consolidates(tmp_path: Path, gc_runs: list[str]) -> None:
    repo = GitopsRepo(
        local_path=str(tmp_path / "work"),
        push=False,
        pack=GitopsPackSettings(loose_objects=3, packs=0),
    )
    repo.ensure()
    for i in range(8):
        repo.publish({"r.yaml": f"n: {i}\n"}, message=f"rule {i}")
    before = _packs(repo)

    assert repo.consolidate_if_due() is False

    assert _packs(repo) == before > 1
    assert gc_runs == []


async def test_the_background_task_consolidates_until_cancelled(tmp_path: Path) -> None:
    repo = _packed_clone(tmp_path, writes=8)
    task = asyncio.create_task(consolidate_forever(repo, 0.01))
    try:
        async with asyncio.timeout(10):
            while _packs(repo) != 1:
                await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert _packs(repo) == 1
