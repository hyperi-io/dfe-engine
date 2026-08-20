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

if TYPE_CHECKING:
    from dfe_engine.auth.accounts import Account, AccountStore, FerretDBAccountStore
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

    enabled: bool = Field(description="Deploy-repo persistence is wired (gitops enabled).")
    auto_merge: bool = Field(description="Effective auto-merge: changes commit straight to main.")
    merged: bool = Field(
        description="The durable copy matches the live account -- survives a rebuild."
    )
    pending: AccountGitPending | None = Field(
        default=None, description="Present when merged is false: the action to make it durable."
    )


# The git-disabled deployment: the live store is a plain file share, so it is
# already durable and there is nothing to merge.
_FILE_SHARE_STATE = AccountGitState(enabled=False, auto_merge=False, merged=True, pending=None)


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
        return AccountGitState(
            enabled=True,
            auto_merge=bool(outcome and outcome.auto_merged),
            merged=True,
            pending=None,
        )
    pending = AccountGitPending(
        pr_url=outcome.pr_url,
        branch=outcome.branch,
        command=None if outcome.pr_url else _cli_merge_command(gc, outcome.branch or ""),
    )
    return AccountGitState(enabled=True, auto_merge=False, merged=False, pending=pending)


# ── Steady-state read (setup-status) ─────────────────────────


def steady_state(
    gc: GitCrud | None,
    account_store: AccountStore | FerretDBAccountStore | None,
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
    merged = (
        stored is not None
        and live is not None
        and stored.get("password_hash") == live.password_hash
    )
    return AccountGitState(enabled=True, auto_merge=state.effective, merged=merged, pending=None)


# ── Boot hydration ───────────────────────────────────────────


def hydrate_from_deploy_repo(
    gc: GitCrud | None,
    account_store: AccountStore | FerretDBAccountStore,
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


def publish_seed(
    gc: GitCrud | None,
    account: Account,
) -> None:
    """Direct-commit a freshly seeded account into the deploy repo (best-effort).

    The initial break-glass admin is seeded at boot; persisting it straight away
    means the emergency credential is durable from the first start, not only after
    an operator rotates it. Direct commit (not routed): a review PR nobody merges
    would leave the break-glass out of the durable copy.
    """
    if gc is None:
        return
    doc = account.model_dump(exclude={"username"})
    try:
        gc.put(
            ACCOUNTS_CLASS,
            account.username,
            doc,
            "dfe-engine",
            _commit_message(account.username, "seed account", "dfe-engine", ""),
        )
    except Exception as exc:  # durability is best-effort; never break startup
        logger.warning("Account seed not persisted to the deploy repo", error=str(exc))
