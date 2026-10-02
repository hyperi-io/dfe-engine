#  Project:      dfe-engine
#  File:         tests/governance/test_seed.py
#  Purpose:      The shipped action library reaches a deploy repo that lacks it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The seed puts the standard dials in an empty deploy repo and then gets out of the way.

The failure this guards against is not a crash - it is Governed Ops answering
200 with an empty list on a fresh deployment, which reads as "the feature is
broken" to whoever opens the UI. The second failure it guards against is the
opposite: a seed that keeps re-asserting itself and overwrites the detents an
operator deliberately changed.
"""

from pathlib import Path

import pytest

from dfe_engine.governance.models import ActionDef
from dfe_engine.governance.seed import (
    ACTIONS_SUBDIR,
    IGNORE_FILE,
    POLICIES_SUBDIR,
    pending_seed,
)
from dfe_engine.yaml_utils import yaml_load_string


class TestEmptyRepo:
    def test_seeds_actions_and_policies(self, tmp_path: Path):
        artifacts = pending_seed(repo_root=tmp_path)

        actions = [k for k in artifacts if k.startswith(ACTIONS_SUBDIR)]
        policies = [k for k in artifacts if k.startswith(POLICIES_SUBDIR)]

        assert actions, "an empty deploy repo must be given the shipped actions"
        assert policies, "an empty deploy repo must be given the baseline policy"

    def test_ships_the_storage_layout_lock(self, tmp_path: Path):
        """Every deployment gets the storage lock, not just a hand-seeded one."""
        artifacts = pending_seed(repo_root=tmp_path)
        names = {Path(k).stem for k in artifacts if k.startswith(POLICIES_SUBDIR)}
        assert names == {"baseline", "sizing-locks", "storage-layout"}

        doc = yaml_load_string(artifacts[f"{POLICIES_SUBDIR}/storage-layout.yaml"])
        assert "infravars:*:kafka.storage.size" in doc["protected"]
        assert "infravars:*:clickhouse.storage.storageClass" in doc["protected"]
        # One dial block per storage family, both services: tiering MOVES parts onto
        # the cold volume, so its name, class and size lock like the bucket does.
        for block in (
            "clickhouse.objectStore.*",
            "clickhouse.tieredBlock.*",
            "kafka.objectStore.*",
            "kafka.tieredObject.*",
        ):
            assert f"infravars:*:{block}" in doc["protected"]

    def test_the_lock_lands_beside_a_repo_that_already_has_the_old_one(self, tmp_path: Path):
        """A seeded policy file is never rewritten, so a renamed vocabulary reaches
        an existing deploy repo only under a new filename."""
        stale = tmp_path / POLICIES_SUBDIR / "storage-model.yaml"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(
            'name: storage-model\nprotected:\n  - "infravars:*:clickhouse.s3.*"\n',
            encoding="utf-8",
            newline="\n",
        )
        artifacts = pending_seed(repo_root=tmp_path)
        names = {Path(k).stem for k in artifacts if k.startswith(POLICIES_SUBDIR)}
        assert "storage-layout" in names
        assert "storage-model" not in names
        assert stale.read_text(encoding="utf-8").startswith("name: storage-model")

    def test_ships_the_sizing_lock(self, tmp_path: Path):
        """A deploy repo not made from the template still gets the sizing locks."""
        artifacts = pending_seed(repo_root=tmp_path)
        doc = yaml_load_string(artifacts[f"{POLICIES_SUBDIR}/sizing-locks.yaml"])
        assert doc["name"] == "sizing-locks"
        assert doc["protected"] == [
            "infravars:*:kafka.sizing.*",
            "infravars:*:kafka.controllerPool.enabled",
            "infravars:*:cloud",
        ]

    def test_the_lock_carries_the_pre_vocabulary_spellings(self, tmp_path: Path):
        """A deploy repo pinned to an older chart still writes these paths."""
        artifacts = pending_seed(repo_root=tmp_path)
        doc = yaml_load_string(artifacts[f"{POLICIES_SUBDIR}/storage-layout.yaml"])
        for old in ("clickhouse.s3.*", "clickhouse.tiered.*", "kafka.tiered.*"):
            assert f"infravars:*:{old}" in doc["protected"]

    def test_ships_the_documented_dials(self, tmp_path: Path):
        artifacts = pending_seed(repo_root=tmp_path)
        names = {Path(k).stem for k in artifacts if k.startswith(ACTIONS_SUBDIR)}

        # The library FORK-level docs and dfe-deploy both promise these five.
        assert names == {
            "hunts-pause",
            "hunts-resume",
            "hunts-throttle",
            "receiver-normal",
            "receiver-surge",
        }

    def test_seeded_actions_parse_as_real_action_defs(self, tmp_path: Path):
        """Shipping a file the engine cannot load would be worse than shipping none."""
        artifacts = pending_seed(repo_root=tmp_path)

        for rel, content in artifacts.items():
            if not rel.startswith(ACTIONS_SUBDIR):
                continue
            ActionDef.model_validate(yaml_load_string(content))

    def test_seeded_content_is_not_empty(self, tmp_path: Path):
        for content in pending_seed(repo_root=tmp_path).values():
            assert content.strip(), "seeded a blank file"


class TestExistingRepo:
    def _seed_into(self, root: Path) -> None:
        for rel, content in pending_seed(repo_root=root).items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def test_second_run_seeds_nothing(self, tmp_path: Path):
        self._seed_into(tmp_path)
        assert pending_seed(repo_root=tmp_path) == {}

    def test_operator_edits_are_never_overwritten(self, tmp_path: Path):
        self._seed_into(tmp_path)
        edited = tmp_path / ACTIONS_SUBDIR / "receiver-surge.yaml"
        edited.write_text("name: receiver-surge\n# operator tuned this\n", encoding="utf-8")

        assert pending_seed(repo_root=tmp_path) == {}
        assert "operator tuned this" in edited.read_text(encoding="utf-8")

    def test_a_newly_shipped_action_lands_beside_customised_ones(self, tmp_path: Path):
        """A later engine release must be able to add a dial without a reset."""
        self._seed_into(tmp_path)
        (tmp_path / ACTIONS_SUBDIR / "receiver-surge.yaml").write_text(
            "name: receiver-surge\n# customised\n", encoding="utf-8"
        )
        # Simulate "this one is new in the release" by removing it locally.
        (tmp_path / ACTIONS_SUBDIR / "hunts-throttle.yaml").unlink()

        artifacts = pending_seed(repo_root=tmp_path)

        assert set(artifacts) == {f"{ACTIONS_SUBDIR}/hunts-throttle.yaml"}
        assert "customised" in (tmp_path / ACTIONS_SUBDIR / "receiver-surge.yaml").read_text(
            encoding="utf-8"
        )


class TestDeletionTombstone:
    def test_a_plain_deletion_comes_back(self, tmp_path: Path):
        """Deliberate, and documented: without a tombstone we cannot tell a
        deletion from a repo that was never seeded."""
        for rel, content in pending_seed(repo_root=tmp_path).items():
            target = tmp_path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (tmp_path / ACTIONS_SUBDIR / "hunts-pause.yaml").unlink()

        assert f"{ACTIONS_SUBDIR}/hunts-pause.yaml" in pending_seed(repo_root=tmp_path)

    def test_seed_ignore_keeps_it_gone(self, tmp_path: Path):
        ignore = tmp_path / IGNORE_FILE
        ignore.parent.mkdir(parents=True, exist_ok=True)
        ignore.write_text("# we do not want this dial\nhunts-pause\n", encoding="utf-8")

        artifacts = pending_seed(repo_root=tmp_path)

        assert f"{ACTIONS_SUBDIR}/hunts-pause.yaml" not in artifacts
        assert artifacts, "ignoring one action must not suppress the rest"

    @pytest.mark.parametrize("line", ["", "   ", "# just a comment"])
    def test_blank_and_comment_lines_are_not_names(self, tmp_path: Path, line: str):
        ignore = tmp_path / IGNORE_FILE
        ignore.parent.mkdir(parents=True, exist_ok=True)
        ignore.write_text(f"{line}\n", encoding="utf-8")

        assert len(pending_seed(repo_root=tmp_path)) == 8
