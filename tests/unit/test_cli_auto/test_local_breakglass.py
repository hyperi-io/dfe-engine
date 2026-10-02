#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_local_breakglass.py
#  Purpose:      End-to-end tests for the `dfe local` break-glass command group
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe local`` break-glass CRUD - exercised end to end against a REAL git repo.

No mocks: each test seeds a real dulwich gitops clone in ``tmp_path`` (a helm-vars
overlay + a versioned ch-tiers envelope), drives the click commands with ``--repo``
pointed at it, and asserts on the real commit history / working tree. Covers the
break-glass marking (subject prefix + trailers), the required-reason rule, the
guard confirm, the audit-log flag, and revert/rm/restore/grep.
"""

import io
import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from dulwich import porcelain
from dulwich.repo import Repo

from dfe_engine.cli.auto.local import local_group
from dfe_engine.settings import reset_settings

_HELM = "keda:\n  minReplicas: 1\n  maxReplicas: 5\nother: keep\n"
_TIER = (
    "current: 2\n"
    "deployed: null\n"
    "status: published\n"
    "draft: null\n"
    "versions:\n"
    "  1:\n"
    "    by: seed\n"
    "    message: v1\n"
    "    spec:\n"
    "      max_rows: 100\n"
    "  2:\n"
    "    by: seed\n"
    "    message: v2\n"
    "    spec:\n"
    "      max_rows: 200\n"
)


@pytest.fixture(autouse=True)
def _hermetic_gitops(monkeypatch):
    """Keep get_settings() offline + default: clear any DFE_GITOPS_* + reset cache.

    ``load_settings`` re-reads ``.env`` with ``override=False``, so deleting
    ``DFE_GITOPS_REPO_URL`` lets the local dotenv put it back and ``dfe local
    push`` tries a real remote. An empty value stays empty.
    """
    for key in list(os.environ):
        if key.startswith("DFE_GITOPS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DFE_GITOPS_REPO_URL", "")
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real gitops clone: a helmvars overlay + a versioned ch_tiers envelope."""
    porcelain.init(str(tmp_path))
    values = tmp_path / "values"
    values.mkdir()
    (values / "receiver-default.yaml").write_text(_HELM)
    tiers = tmp_path / "governance" / "ch" / "tiers"
    tiers.mkdir(parents=True)
    (tiers / "gold.yaml").write_text(_TIER)
    porcelain.add(
        str(tmp_path),
        paths=[str(values / "receiver-default.yaml"), str(tiers / "gold.yaml")],
    )
    porcelain.commit(
        str(tmp_path),
        message=b"seed: fixtures",
        author=b"seed <seed@example.com>",
        committer=b"seed <seed@example.com>",
    )
    return tmp_path


def _run(repo: Path, args: list[str], **kwargs):
    return CliRunner().invoke(local_group, [*args, "--repo", str(repo)], **kwargs)


def _head_message(repo: Path) -> str:
    with Repo(str(repo)) as r:
        return r[r.head()].message.decode()


# --- read commits ------------------------------------------------------------


def test_classes_lists_registry(repo: Path):
    result = _run(repo, ["classes"])
    assert result.exit_code == 0, result.output
    assert "helmvars" in result.output
    assert "ch_tiers" in result.output


def test_ls_and_get(repo: Path):
    ls = _run(repo, ["ls", "helmvars"])
    assert ls.exit_code == 0, ls.output
    assert "receiver-default" in ls.output

    got = _run(repo, ["get", "helmvars", "receiver-default", "--path", "keda.maxReplicas"])
    assert got.exit_code == 0, got.output
    assert got.output.strip() == "5"


def test_grep_finds_term(repo: Path):
    result = _run(repo, ["grep", "maxReplicas"])
    assert result.exit_code == 0, result.output
    assert "receiver-default.yaml" in result.output


def test_versions_lists_published(repo: Path):
    result = _run(repo, ["versions", "ch_tiers", "gold"])
    assert result.exit_code == 0, result.output
    assert "1" in result.output.split()
    assert "2" in result.output.split()


# --- break-glass marking + guard ---------------------------------------------


def test_set_writes_break_glass_commit(repo: Path):
    result = _run(
        repo,
        [
            "set",
            "helmvars",
            "receiver-default",
            "keda.maxReplicas",
            "0",
            "--reason",
            "rogue pod",
            "--yes",
            "--no-push",
        ],
    )
    assert result.exit_code == 0, result.output

    # The value landed via GitCrud.
    got = _run(repo, ["get", "helmvars", "receiver-default", "--path", "keda.maxReplicas"])
    assert got.output.strip() == "0"

    # The commit is unmistakably break-glass (subject prefix + all three trailers).
    msg = _head_message(repo)
    assert msg.startswith("[BREAK-GLASS] ")
    assert msg.splitlines()[0].startswith("[BREAK-GLASS] cfg(receiver-default):")
    assert "DFE-Break-Glass: true" in msg
    assert "DFE-Actor:" in msg
    assert "DFE-Reason: rogue pod" in msg


def test_set_requires_reason_non_interactive(repo: Path):
    result = _run(
        repo,
        ["set", "helmvars", "receiver-default", "keda.maxReplicas", "0", "--yes", "--no-push"],
    )
    assert result.exit_code != 0
    assert "reason is required" in result.output


def test_guard_confirm_yes_proceeds(repo: Path):
    # No --yes: the default y/N confirm runs; feed "y".
    result = _run(
        repo,
        [
            "set",
            "helmvars",
            "receiver-default",
            "keda.maxReplicas",
            "0",
            "--reason",
            "outage",
            "--no-push",
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "writing to" in result.output
    assert _head_message(repo).startswith("[BREAK-GLASS] ")


def test_guard_confirm_abort_no_write(repo: Path):
    result = _run(
        repo,
        [
            "set",
            "helmvars",
            "receiver-default",
            "keda.maxReplicas",
            "0",
            "--reason",
            "outage",
            "--no-push",
        ],
        input="n\n",
    )
    assert result.exit_code != 0
    # Nothing was committed - HEAD is still the seed commit.
    assert _head_message(repo).startswith("seed:")


def test_safety_guard_steers_but_can_override(repo: Path):
    # replicaCount is KEDA-owned - the SAME validate_change the API runs flags it.
    # Break-glass shows the steer (use keda.minReplicaCount/maxReplicaCount) but --yes overrides.
    result = _run(
        repo,
        [
            "set",
            "helmvars",
            "receiver-default",
            "replicaCount",
            "0",
            "--reason",
            "rogue pod",
            "--yes",
            "--no-push",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "safety:" in result.output
    assert "keda.minReplicaCount/maxReplicaCount" in result.output  # the persona steer
    # Overridden -> the value was written despite the warning.
    assert _head_message(repo).startswith("[BREAK-GLASS]")


def test_safety_guard_reads_every_leaf_of_a_map_value(repo: Path):
    result = _run(
        repo,
        [
            "set",
            "helmvars",
            "receiver-default",
            "image",
            '{"tag": "latest"}',
            "--reason",
            "x",
            "--no-push",
        ],
        input="n\n",  # decline "Override this safety guard?"
    )
    assert result.exit_code != 0
    assert "safety: unpinned/floating image ref at image.tag" in result.output
    assert _head_message(repo).startswith("seed:")  # nothing committed


def test_safety_guard_decline_aborts(repo: Path):
    # No --yes: declining the safety override aborts before anything is committed.
    result = _run(
        repo,
        ["set", "helmvars", "receiver-default", "replicaCount", "0", "--reason", "x", "--no-push"],
        input="n\n",  # decline "Override this safety guard?"
    )
    assert result.exit_code != 0
    assert _head_message(repo).startswith("seed:")  # nothing committed


def test_log_flags_break_glass(repo: Path):
    _run(
        repo,
        [
            "set",
            "helmvars",
            "receiver-default",
            "keda.maxReplicas",
            "0",
            "--reason",
            "rogue pod",
            "--yes",
            "--no-push",
        ],
    )
    result = _run(repo, ["log"])
    assert result.exit_code == 0, result.output
    assert "!! BREAK-GLASS" in result.output


# --- mutating ops ------------------------------------------------------------


def test_unset_removes_key(repo: Path):
    result = _run(
        repo,
        [
            "unset",
            "helmvars",
            "receiver-default",
            "keda.maxReplicas",
            "--reason",
            "revert dial",
            "--yes",
            "--no-push",
        ],
    )
    assert result.exit_code == 0, result.output
    got = _run(repo, ["get", "helmvars", "receiver-default", "--path", "keda.maxReplicas"])
    assert got.exit_code != 0  # key is gone
    assert _head_message(repo).startswith("[BREAK-GLASS] ")


def test_revert_undoes_a_commit(repo: Path):
    setres = _run(
        repo,
        [
            "set",
            "helmvars",
            "receiver-default",
            "keda.maxReplicas",
            "0",
            "--reason",
            "rogue pod",
            "--yes",
            "--no-push",
        ],
    )
    assert setres.exit_code == 0, setres.output
    with Repo(str(repo)) as r:
        bad_sha = r.head().decode()

    rev = _run(repo, ["revert", bad_sha, "--reason", "undo the scale", "--yes", "--no-push"])
    assert rev.exit_code == 0, rev.output

    got = _run(repo, ["get", "helmvars", "receiver-default", "--path", "keda.maxReplicas"])
    assert got.output.strip() == "5"  # back to the seeded value
    msg = _head_message(repo)
    assert msg.startswith("[BREAK-GLASS] ")
    assert "revert" in msg.splitlines()[0]


def test_rm_deletes_resource(repo: Path):
    result = _run(
        repo,
        ["rm", "helmvars", "receiver-default", "--reason", "remove overlay", "--yes", "--no-push"],
    )
    assert result.exit_code == 0, result.output
    ls = _run(repo, ["ls", "helmvars"])
    assert "receiver-default" not in ls.output
    assert _head_message(repo).startswith("[BREAK-GLASS] ")


def test_restore_rolls_back_version(repo: Path):
    result = _run(
        repo,
        ["restore", "ch_tiers", "gold", "1", "--reason", "bad tier", "--yes", "--no-push"],
    )
    assert result.exit_code == 0, result.output
    got = _run(repo, ["get", "ch_tiers", "gold", "--path", "current"])
    assert got.output.strip() == "1"
    assert _head_message(repo).startswith("[BREAK-GLASS] ")


def test_push_no_remote_is_noop(repo: Path):
    result = _run(repo, ["push"])
    assert result.exit_code == 0, result.output
    assert "no remote" in result.output.lower() or "nothing" in result.output.lower()


@pytest.fixture
def remote(repo: Path, tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A bare deploy repo holding the clone's seed commit, configured as the remote."""
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    bare = deploy_dir / "remote.git"
    porcelain.init(str(bare), bare=True)
    branch = porcelain.active_branch(str(repo)).decode()
    porcelain.push(str(repo), str(bare), f"refs/heads/{branch}".encode(), errstream=io.BytesIO())
    monkeypatch.setenv("DFE_GITOPS_REPO_URL", str(bare))
    monkeypatch.setenv("DFE_GITOPS_BRANCH", branch)
    reset_settings()
    return bare, branch


def _break_glass_set(repo: Path, value: str) -> None:
    result = _run(
        repo,
        ["set", "helmvars", "receiver-default", "keda.maxReplicas", value]
        + ["--reason", "rogue pod", "--yes", "--no-push"],
    )
    assert result.exit_code == 0, result.output


def _remote_head(bare: Path, branch: str) -> bytes:
    with Repo(str(bare)) as r:
        return r.refs[f"refs/heads/{branch}".encode()]


def test_push_lands_break_glass_commits_on_the_remote(repo: Path, remote: tuple[Path, str]):
    """The commit made with push off is pushed, not reset away as a lost push race."""
    bare, branch = remote
    _break_glass_set(repo, "0")
    with Repo(str(repo)) as r:
        break_glass = r.head()

    result = _run(repo, ["push"])

    assert result.exit_code == 0, result.output
    assert "pushed to" in result.output
    assert _remote_head(bare, branch) == break_glass
    got = _run(repo, ["get", "helmvars", "receiver-default", "--path", "keda.maxReplicas"])
    assert got.output.strip() == "0"


def test_push_refuses_a_moved_remote_and_keeps_the_commits(
    repo: Path, remote: tuple[Path, str], tmp_path: Path
):
    """Another writer moved the remote: nothing is pushed and the local commit stays."""
    bare, branch = remote
    _break_glass_set(repo, "0")
    with Repo(str(repo)) as r:
        break_glass = r.head()
    other = tmp_path / "other-clone"
    porcelain.clone(str(bare), str(other), branch=branch.encode(), errstream=io.BytesIO())
    (other / "elsewhere.yaml").write_text("x: 1\n")
    porcelain.add(str(other), paths=[str(other / "elsewhere.yaml")])
    porcelain.commit(str(other), message=b"another writer", author=b"t <t@t>", committer=b"t <t@t>")
    porcelain.push(str(other), str(bare), f"refs/heads/{branch}".encode(), errstream=io.BytesIO())
    moved = _remote_head(bare, branch)

    result = _run(repo, ["push"])

    assert result.exit_code != 0, result.output
    assert "Nothing was pushed" in result.output
    assert _remote_head(bare, branch) == moved
    with Repo(str(repo)) as r:
        assert r.head() == break_glass


def test_ch_cloud_is_mounted():
    assert "ch-cloud" in local_group.commands
