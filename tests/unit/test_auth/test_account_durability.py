#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_account_durability.py
#  Purpose:      Cover the account durability layer across all persistence modes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Account durability: file-share, dev-direct, auto-merge, prod PR, prod CLI, hydrate.

The layer mirrors a live account change into the deploy repo's governed ``accounts``
class and reports whether the durable copy has caught up. These tests drive a REAL
``GitCrud`` over a tmp local repo (no mocks of the git mechanics) so the routing --
direct commit vs review PR vs CLI fallback -- is exercised end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dulwich import porcelain

from dfe_engine.auth import account_durability as ad
from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.gitcrud.auto_merge import set_stored
from dfe_engine.gitcrud.engine import GitCrud, ResourceNotFoundError
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitops.repo import GitopsRepo

# A well-formed-looking bcrypt hash: durability compares hash STRINGS, never verifies.
_HASH_A = "$2b$12$" + "A" * 53
_HASH_B = "$2b$12$" + "B" * 53

DEV = "local"  # is_dev_posture(): dev/local/test/ci
PROD = "production"


def _gc(tmp_path: Path, sub: str = "deploy") -> GitCrud:
    """A GitCrud over a fresh local-init deploy repo (no remote, no push)."""
    repo = GitopsRepo(local_path=str(tmp_path / sub), repo_url="", push=False)
    return GitCrud(repo)


def _account(username: str = "admin", password_hash: str = _HASH_A) -> Account:
    return Account(username=username, password_hash=password_hash, groups=["dfe-admins"])


def _doc(account: Account) -> dict:
    return account.model_dump(exclude={"username"})


class _FakeForge:
    """Records PR opens and returns a fixed PR (stands in for Forgejo/GitHub)."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> PullRequest:
        self.opened.append((head, base))
        return PullRequest(number=7, url="http://forge.local/pr/7", branch=head)


# ── Git-disabled (file share) ────────────────────────────────


def test_disabled_publish_returns_none_and_file_share_state():
    outcome = ad.publish_account(
        None,
        None,
        environment=DEV,
        mode="solo",
        username="admin",
        doc=_doc(_account()),
        summary="reset password",
        actor="admin",
    )
    assert outcome is None
    state = ad.state_from_outcome(None, outcome)
    assert state.enabled is False
    assert state.merged is True  # a file share is already durable
    assert state.pending is None


def test_disabled_steady_state_is_durable(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    store.create("admin", "pw")
    state = ad.steady_state(None, store, "admin", environment=DEV, mode="solo")
    assert state == account_durability_file_share()


def account_durability_file_share():
    return ad.AccountGitState(enabled=False, auto_merge=False, merged=True, pending=None)


# ── Dev / solo: direct commit, immediately durable ───────────


def test_dev_direct_commit_is_merged(tmp_path):
    gc = _gc(tmp_path)
    outcome = ad.publish_account(
        gc,
        None,
        environment=DEV,
        mode="solo",
        username="admin",
        doc=_doc(_account()),
        summary="reset password",
        actor="admin",
    )
    assert outcome is not None
    assert outcome.changed is True
    assert outcome.review_required is False
    state = ad.state_from_outcome(gc, outcome)
    assert state.enabled is True
    assert state.merged is True
    assert state.pending is None
    # The account landed on the tracked branch (working tree), not a side branch.
    assert gc.get(ad.ACCOUNTS_CLASS, "admin")["password_hash"] == _HASH_A


def test_dev_autonmerge_flag_reports_auto_merged(tmp_path):
    gc = _gc(tmp_path)
    set_stored(gc, True, "tester")  # commit the auto_merge flag ON
    outcome = ad.publish_account(
        gc,
        None,
        environment=DEV,
        mode="solo",
        username="admin",
        doc=_doc(_account()),
        summary="reset password",
        actor="admin",
    )
    assert outcome is not None
    assert outcome.auto_merged is True
    assert ad.state_from_outcome(gc, outcome).auto_merge is True


# ── Production + team: review PR, pending until merged ───────


def test_prod_team_opens_pr_and_is_pending(tmp_path):
    gc = _gc(tmp_path)
    forge = _FakeForge()
    # A base commit must exist before a review branch can fork off it.
    ad.publish_seed(gc, _account(password_hash=_HASH_A))

    outcome = ad.publish_account(
        gc,
        forge,
        environment=PROD,
        mode="team",
        username="admin",
        doc=_doc(_account(password_hash=_HASH_B)),
        summary="reset password",
        actor="admin",
    )
    assert outcome is not None
    assert outcome.review_required is True
    assert forge.opened, "a PR should have been opened"
    state = ad.state_from_outcome(gc, outcome)
    assert state.merged is False
    assert state.pending is not None
    assert state.pending.pr_url == "http://forge.local/pr/7"
    assert state.pending.command is None  # a forge PR, not the CLI fallback
    # main is untouched: the tracked branch still holds the OLD hash (PR pending).
    assert gc.get(ad.ACCOUNTS_CLASS, "admin")["password_hash"] == _HASH_A


def test_prod_team_no_forge_falls_back_to_cli(tmp_path):
    gc = _gc(tmp_path)
    ad.publish_seed(gc, _account(password_hash=_HASH_A))  # base commit

    outcome = ad.publish_account(
        gc,
        None,
        environment=PROD,
        mode="team",  # no forge configured
        username="admin",
        doc=_doc(_account(password_hash=_HASH_B)),
        summary="reset password",
        actor="admin",
    )
    assert outcome is not None
    assert outcome.review_required is True
    assert outcome.branch  # committed to a review branch, never main
    state = ad.state_from_outcome(gc, outcome)
    assert state.merged is False
    assert state.pending is not None
    assert state.pending.pr_url is None
    assert state.pending.command is not None
    assert "merge" in state.pending.command
    assert gc.get(ad.ACCOUNTS_CLASS, "admin")["password_hash"] == _HASH_A  # main unchanged


# ── Steady state (setup-status) ──────────────────────────────


def test_steady_state_merged_when_git_matches_live(tmp_path):
    gc = _gc(tmp_path)
    store = AccountStore(tmp_path / "accounts")
    store.put(_account(password_hash=_HASH_A))
    ad.publish_seed(gc, _account(password_hash=_HASH_A))

    state = ad.steady_state(gc, store, "admin", environment=DEV, mode="solo")
    assert state.enabled is True
    assert state.merged is True


def test_steady_state_unmerged_when_live_hash_ahead(tmp_path):
    gc = _gc(tmp_path)
    store = AccountStore(tmp_path / "accounts")
    ad.publish_seed(gc, _account(password_hash=_HASH_A))  # deploy repo has A
    store.put(_account(password_hash=_HASH_B))  # live store rotated to B

    state = ad.steady_state(gc, store, "admin", environment=DEV, mode="solo")
    assert state.merged is False


def test_steady_state_unmerged_when_not_yet_persisted(tmp_path):
    gc = _gc(tmp_path)
    store = AccountStore(tmp_path / "accounts")
    store.put(_account(password_hash=_HASH_A))  # live only, nothing in the deploy repo
    state = ad.steady_state(gc, store, "admin", environment=DEV, mode="solo")
    assert state.merged is False


# ── Hydration + seed ─────────────────────────────────────────


def test_hydrate_restores_accounts_from_deploy_repo(tmp_path):
    gc = _gc(tmp_path)
    ad.publish_seed(gc, _account(username="admin", password_hash=_HASH_A))
    ad.publish_seed(gc, _account(username="kaz", password_hash=_HASH_B))

    fresh = AccountStore(tmp_path / "fresh-accounts")  # empty, as after a rebuild
    assert fresh.list() == []
    restored = ad.hydrate_from_deploy_repo(gc, fresh)

    assert restored == 2
    assert fresh.get("admin").password_hash == _HASH_A
    assert fresh.get("kaz").password_hash == _HASH_B


def test_hydrate_is_a_noop_when_gitops_disabled(tmp_path):
    fresh = AccountStore(tmp_path / "accounts")
    assert ad.hydrate_from_deploy_repo(None, fresh) == 0


def test_publish_seed_persists_the_account(tmp_path):
    gc = _gc(tmp_path)
    ad.publish_seed(gc, _account(password_hash=_HASH_A))
    assert gc.get(ad.ACCOUNTS_CLASS, "admin")["password_hash"] == _HASH_A


def test_publish_seed_noop_when_disabled():
    ad.publish_seed(None, _account())  # must not raise


# ── Delete ───────────────────────────────────────────────────


def test_remove_account_deletes_from_deploy_repo(tmp_path):
    gc = _gc(tmp_path)
    ad.publish_seed(gc, _account(password_hash=_HASH_A))
    assert gc.get(ad.ACCOUNTS_CLASS, "admin")["password_hash"] == _HASH_A

    outcome = ad.remove_account(
        gc,
        None,
        environment=DEV,
        mode="solo",
        username="admin",
        actor="admin",
    )
    assert outcome is not None
    assert outcome.changed is True
    with pytest.raises(ResourceNotFoundError):
        gc.get(ad.ACCOUNTS_CLASS, "admin")


def test_remove_account_noop_when_disabled():
    assert (
        ad.remove_account(None, None, environment=DEV, mode="solo", username="admin", actor="admin")
        is None
    )


# ── Remote merge poll (step 2 of the review-PR path) ─────────


def test_remote_state_file_share_when_disabled(tmp_path):
    store = AccountStore(tmp_path / "accounts")
    store.put(_account())
    state = ad.remote_state(None, store, "admin", environment=DEV, mode="solo")
    assert state.enabled is False
    assert state.merged is True


def test_remote_state_uses_working_tree_when_no_remote(tmp_path):
    gc = _gc(tmp_path)  # local-init repo, no remote -> the working tree is the truth
    store = AccountStore(tmp_path / "accounts")
    store.put(_account(password_hash=_HASH_A))
    ad.publish_seed(gc, _account(password_hash=_HASH_A))
    assert ad.remote_state(gc, store, "admin", environment=DEV, mode="solo").merged is True
    store.put(_account(password_hash=_HASH_B))
    assert ad.remote_state(gc, store, "admin", environment=DEV, mode="solo").merged is False


def test_remote_state_flips_merged_when_remote_catches_up(tmp_path):
    # A remote deploy repo with the admin persisted at hash A.
    remote = GitopsRepo(local_path=str(tmp_path / "remote"), repo_url="", push=False)
    remote_gc = GitCrud(remote)
    ad.publish_seed(remote_gc, _account(password_hash=_HASH_A))
    branch = porcelain.active_branch(str(remote.path)).decode()

    # The engine clones that remote.
    engine = GitopsRepo(
        local_path=str(tmp_path / "engine"), repo_url=str(remote.path), push=False, branch=branch
    )
    engine_gc = GitCrud(engine)

    # The engine rotated the password to B and opened a PR; remote main still has A.
    store = AccountStore(tmp_path / "live")
    store.put(_account(password_hash=_HASH_B))
    pending = ad.remote_state(engine_gc, store, "admin", environment=PROD, mode="team")
    assert pending.enabled is True
    assert pending.merged is False

    # The operator merges the PR: remote main advances to B.
    ad.publish_seed(remote_gc, _account(password_hash=_HASH_B))
    confirmed = ad.remote_state(engine_gc, store, "admin", environment=PROD, mode="team")
    assert confirmed.merged is True


def test_read_remote_file_none_without_remote(tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "local"), repo_url="", push=False)
    assert repo.has_remote is False
    assert repo.read_remote_file("governance/rbac/accounts/admin.yaml") is None


# ── Break-glass gating (only the admin is git-persisted) ─────


def test_is_break_glass_matches_the_admin(monkeypatch):
    monkeypatch.delenv("DFE_AUTH_LOCAL_ADMIN_NAME", raising=False)
    assert ad.is_break_glass("admin") is True
    assert ad.is_break_glass("alice") is False


def test_is_break_glass_follows_env_admin_name(monkeypatch):
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_NAME", "root")
    assert ad.is_break_glass("root") is True
    assert ad.is_break_glass("admin") is False


def test_not_git_backed_state_is_durable():
    state = ad.not_git_backed_state()
    assert state.enabled is False
    assert state.merged is True
    assert state.pending is None
