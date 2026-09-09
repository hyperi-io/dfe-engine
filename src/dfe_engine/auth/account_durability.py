#  Project:      dfe-engine
#  File:         auth/account_durability.py
#  Purpose:      Persist local-account changes into the deploy repo (survive rebuild)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Durability layer for local accounts: mirror the live store into the deploy repo.

The live ``AccountStore`` (``auth_dir/accounts``) is the auth source of truth and
takes effect immediately -- a password reset is live on the next login. That
directory is ephemeral on a container rebuild, so on its own the credential is
lost the moment the pod is recreated.

This module mirrors every account mutation into the deploy repo's governed
``accounts`` class (``governance/rbac/accounts``), which is the durable SSoT, and
hydrates the live store back from it on boot. The mirror goes through the normal
governed-write routing (``gitcrud/routing.py``): a dev/solo deployment commits
straight to main (auto-merge), a production+team deployment opens a review PR --
or, when no forge is configured, hands back the git commands to merge the branch.

When gitops is disabled (no deploy repo) the live store IS the durable store -- a
plain file share -- so there is nothing to mirror and ``git_merged`` is trivially
true.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine.gitcrud.auto_merge import resolve_state
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.gitcrud.engine import ResourceNotFoundError
from dfe_engine.gitcrud.routing import (
    ReviewRequiredError,
    WriteOutcome,
    pr_branch_name,
    route_write,
)
from dfe_engine.yaml_utils import yaml_load_string

if TYPE_CHECKING:
    from dfe_engine.auth.accounts import Account, AccountStore, DocuStoreAccountStore
    from dfe_engine.gitcrud.engine import GitCrud
    from dfe_engine.gitcrud.forge import ForgeProvider

# The gitcrud resource class for accounts (registry.py). rbac_prefix "governance",
# so routing forces a review PR on production+team and a direct commit otherwise.
ACCOUNTS_CLASS = "accounts"
_RBAC_CLASS = "governance"


# ── Response / state models ──────────────────────────────────


class AccountGitPending(BaseModel):
    """What the operator must do to make a not-yet-merged change durable."""

    pr_url: str | None = Field(
        default=None, description="Review PR to merge (production+team with a forge)."
    )
    command: str | None = Field(
        default=None,
        description="Git command to merge the review branch, when no forge is configured.",
    )
    branch: str | None = Field(default=None, description="The review branch the change is on.")


class AccountGitState(BaseModel):
    """Durability state of an account change, for the API + the setup wizard."""

    enabled: bool = Field(
        description="This account is git-persisted (the break-glass admin on a gitops "
        "deploy). False for a regular user -- durable in its own store, no git flow."
    )
    auto_merge: bool = Field(description="Effective auto-merge: changes commit straight to main.")
    committed: bool = Field(
        description="The change is committed into git -- a review branch/PR, or main. True as "
        "soon as the write lands, so a pending PR reads as saved (safe), not failed."
    )
    merged: bool = Field(
        description="The durable copy matches the live account -- on main, survives a rebuild. "
        "False while a review PR is committed but unmerged (committed stays true)."
    )
    pending: AccountGitPending | None = Field(
        default=None, description="Present when merged is false: the action to make it durable."
    )


# The git-disabled deployment: the live store is a plain file share, so it is
# already durable and there is nothing to merge. Also the state for a regular
# account, whose durability is its own store (document store/yaml), not git -- so
# it is not committed to git, but is durable (merged) in its own store.
_FILE_SHARE_STATE = AccountGitState(
    enabled=False, auto_merge=False, committed=False, merged=True, pending=None
)


def not_git_backed_state() -> AccountGitState:
    """Durability state for an account that is not git-persisted (its store is durable)."""
    return _FILE_SHARE_STATE


def is_break_glass(username: str, admin_name: str) -> bool:
    """True for the break-glass admin -- the ONLY account git-persisted by default.

    Regular users and groups live in the resolved store (document store/yaml); gitcrud is
    the exception, reserved for the emergency credential that must survive a total
    teardown. ``admin_name`` is the configured name
    (``settings.auth.local.admin_name``, empty string for the default) and is
    required: a caller that omitted it would silently strip the deployment's renamed
    break-glass account of its git-durability path.
    """
    from dfe_engine.auth.bootstrap import admin_account_name

    return username == admin_account_name(admin_name)


# ── Helpers ──────────────────────────────────────────────────


def _cli_merge_command(gc: GitCrud, branch: str) -> str:
    """Git commands the operator runs in the deploy-repo clone to merge a branch."""
    base = gc.repo.branch
    return f"git fetch && git switch {base} && git merge --no-ff {branch} && git push"


def _commit_message(username: str, summary: str, actor: str, request_id: str) -> str:
    """A standard-conforming commit message; username rides an audit trailer.

    The scope stays the fixed ``account`` so the subject never exceeds the 50-char
    limit for a long username -- the username goes in a ``DFE-Account`` trailer.
    """
    return build_message(
        CommitContext(
            ctype="rbac",
            scope="account",
            summary=summary,
            actor=actor,
            request_id=request_id,
            trailers_extra={"DFE-Account": username},
        )
    )


# ── Write path ───────────────────────────────────────────────


def publish_account(
    gc: GitCrud | None,
    forge: ForgeProvider | None,
    *,
    environment: str,
    mode: str,
    username: str,
    doc: dict,
    summary: str,
    actor: str,
    request_id: str = "",
) -> WriteOutcome | None:
    """Mirror one account doc into the deploy repo. None when gitops is disabled.

    Routes through the governed write path: direct-to-main in dev/solo, a review
    PR in production+team. When production+team has no forge, the change is still
    committed to a review branch (never main) and the returned outcome carries the
    branch so the caller can hand back merge commands instead of failing.
    """
    if gc is None:
        return None

    message = _commit_message(username, summary, actor, request_id)

    def _write(branch: str) -> WriteOutcome:
        return gc.put(ACCOUNTS_CLASS, username, doc, actor, message, branch=branch)

    try:
        return route_write(
            gc=gc,
            forge=forge,
            environment=environment,
            mode=mode,
            rbac_class=_RBAC_CLASS,
            resource=f"account/{username}",
            actor=actor,
            title=f"account({username}): {summary}",
            body=(
                f"Persist local account '{username}' ({summary}) by {actor}. "
                "Opened for review because production+team may not commit to main."
            ),
            write=_write,
            request_id=request_id,
        )
    except ReviewRequiredError:
        # Production+team with no forge: commit to a review branch (never main) and
        # let the caller surface the CLI merge commands (Derek's "PR if forge, else CLI").
        branch = pr_branch_name(_RBAC_CLASS, username, request_id)
        res = gc.put(ACCOUNTS_CLASS, username, doc, actor, message, branch=branch)
        logger.warning(
            "REVIEW REQUIRED: account change committed to a branch; no forge to open a PR",
            actor=actor,
            resource=f"account/{username}",
            branch=branch,
        )
        return WriteOutcome(
            changed=res.changed,
            commit_sha=res.commit_sha,
            review_required=True,
            branch=branch,
        )


def remove_account(
    gc: GitCrud | None,
    forge: ForgeProvider | None,
    *,
    environment: str,
    mode: str,
    username: str,
    actor: str,
    request_id: str = "",
) -> WriteOutcome | None:
    """Delete an account from the deploy repo (routed like publish). None when off."""
    if gc is None:
        return None

    message = _commit_message(username, "delete account", actor, request_id)

    def _write(branch: str) -> WriteOutcome:
        return gc.delete(ACCOUNTS_CLASS, username, actor, message, branch=branch)

    try:
        return route_write(
            gc=gc,
            forge=forge,
            environment=environment,
            mode=mode,
            rbac_class=_RBAC_CLASS,
            resource=f"account/{username}",
            actor=actor,
            title=f"account({username}): delete account",
            body=f"Delete local account '{username}' by {actor}.",
            write=_write,
            request_id=request_id,
        )
    except ReviewRequiredError:
        branch = pr_branch_name(_RBAC_CLASS, username, request_id)
        res = gc.delete(ACCOUNTS_CLASS, username, actor, message, branch=branch)
        return WriteOutcome(
            changed=res.changed,
            commit_sha=res.commit_sha,
            review_required=True,
            branch=branch,
        )


def state_from_outcome(gc: GitCrud | None, outcome: WriteOutcome | None) -> AccountGitState:
    """Build the durability state to return alongside a just-performed write."""
    if gc is None:
        return _FILE_SHARE_STATE
    if outcome is None or not outcome.review_required:
        # Direct / effective auto-merge: committed AND merged to main in one step.
        return AccountGitState(
            enabled=True,
            auto_merge=bool(outcome and outcome.auto_merged),
            committed=True,
            merged=True,
            pending=None,
        )
    # Review PR: committed to a branch (safe), not yet merged to main.
    pending = AccountGitPending(
        pr_url=outcome.pr_url,
        branch=outcome.branch,
        command=None if outcome.pr_url else _cli_merge_command(gc, outcome.branch or ""),
    )
    return AccountGitState(
        enabled=True, auto_merge=False, committed=True, merged=False, pending=pending
    )


# ── Steady-state read (setup-status) ─────────────────────────


def steady_state(
    gc: GitCrud | None,
    account_store: AccountStore | DocuStoreAccountStore | None,
    username: str,
    *,
    environment: str,
    mode: str,
) -> AccountGitState:
    """Durability state for a named account with no write in flight (setup-status).

    ``merged`` compares the deploy repo's tracked-branch copy to the live hash: on
    a review-PR deployment the rotated hash sits on a side branch while the tracked
    branch still has the old one, so ``merged`` stays false until the PR is merged
    (and the working clone next pulls it). ``pending`` is not reconstructed here --
    the actionable PR/CLI detail is returned by the reset-password response.
    """
    if gc is None or account_store is None:
        return _FILE_SHARE_STATE

    state = resolve_state(gc, environment=environment, mode=mode, warn=False)
    try:
        stored = gc.get(ACCOUNTS_CLASS, username)
    except ResourceNotFoundError:
        stored = None
    live = account_store.get(username)
    committed = stored is not None  # an account is committed on the tracked branch
    merged = committed and live is not None and stored.get("password_hash") == live.password_hash
    return AccountGitState(
        enabled=True, auto_merge=state.effective, committed=committed, merged=merged, pending=None
    )


def _account_rel(gc: GitCrud, username: str) -> str:
    """The account file's deploy-repo-relative path (governance/rbac/accounts/<u>.yaml)."""
    cls = gc.resource_class(ACCOUNTS_CLASS)
    return f"{cls.directory}/{username}{cls.suffix}"


def remote_state(
    gc: GitCrud | None,
    account_store: AccountStore | DocuStoreAccountStore | None,
    username: str,
    *,
    environment: str,
    mode: str,
) -> AccountGitState:
    """Fetch the deploy repo and report whether main now carries the live password.

    The step-2 poll for the review-PR path: after the operator merges the PR, this
    fetches remote main and flips ``merged`` true -- the running engine sees it
    without a pod re-clone, which the working-tree-only :func:`steady_state` cannot.
    Falls back to :func:`steady_state` when there is no remote (a local-init repo is
    its own truth), and to the file-share state when gitops is off.
    """
    if gc is None or account_store is None:
        return _FILE_SHARE_STATE
    if not gc.repo.has_remote:
        return steady_state(gc, account_store, username, environment=environment, mode=mode)

    state = resolve_state(gc, environment=environment, mode=mode, warn=False)
    content = gc.repo.read_remote_file(_account_rel(gc, username))
    live = account_store.get(username)
    merged = False
    if content is not None and live is not None:
        doc = yaml_load_string(content) or {}
        merged = doc.get("password_hash") == live.password_hash
    # This poll only runs for the git-backed break-glass account after a committing
    # write, so the change is committed (on main if merged, else the review branch).
    return AccountGitState(
        enabled=True, auto_merge=state.effective, committed=True, merged=merged, pending=None
    )


# ── Boot hydration ───────────────────────────────────────────


def hydrate_from_deploy_repo(
    gc: GitCrud | None,
    account_store: AccountStore | DocuStoreAccountStore,
) -> int:
    """Restore the live store from the deploy repo on boot. Returns the count restored.

    The deploy repo is the durable SSoT: a rebuilt pod starts with an empty
    ``auth_dir`` and this puts the persisted accounts back, so a rotated break-glass
    password survives the rebuild. Runs BEFORE the seed-admin-if-empty check, so a
    persisted admin is never overwritten by the shipped default. Best-effort: a
    missing/empty deploy repo just restores nothing.
    """
    if gc is None:
        return 0

    from dfe_engine.auth.accounts import Account

    restored = 0
    for name in gc.list(ACCOUNTS_CLASS):
        try:
            doc = gc.get(ACCOUNTS_CLASS, name)
        except ResourceNotFoundError:
            continue
        account = Account.model_validate({**doc, "username": name})
        account_store.put(account)
        restored += 1
    return restored


def publish_direct(
    gc: GitCrud | None,
    account: Account,
    *,
    summary: str,
    actor: str = "dfe-engine",
) -> None:
    """Direct-commit an account doc into the deploy repo (best-effort, never routed).

    For the changes that have to be durable the moment they happen: the admin
    seeded at boot, and the same account disabled when it is retired. A review PR
    nobody merges would leave the durable copy showing a live admin credential,
    which is the state both of those writes exist to settle.
    """
    if gc is None:
        return
    doc = account.model_dump(exclude={"username"})
    try:
        gc.put(
            ACCOUNTS_CLASS,
            account.username,
            doc,
            actor,
            _commit_message(account.username, summary, actor, ""),
        )
    except Exception as exc:  # durability is best-effort; never break startup
        logger.warning("Account change not persisted to the deploy repo", error=str(exc))
