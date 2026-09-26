#  Project:      dfe-engine
#  File:         auth/admin_retirement.py
#  Purpose:      Retire the bootstrap admin: the deploy-repo fact and its precondition
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Retirement of the bootstrap admin, recorded in the deploy repo.

The deployment mints a random admin password and injects it as
``DFE_AUTH_LOCAL_ADMIN_PASSWORD``. The engine issues that credential with a forced
change at first login and reasserts it whenever the injected value changes
(``auth/bootstrap.py``). That is what makes a rebuild restore the account, and it
is also why the plaintext can never be deleted from the Secret or ``.env`` -- the
engine refuses to start without it, and anyone able to write it can issue
themselves the admin at the next boot.

Retirement ends that. The fact lives beside the break-glass hash in the deploy
repo (``governance/settings/auth.yaml``, key ``admin_retired``), so it survives a
rebuild the same way: with it set, boot skips the admin seed, the account stays
disabled, and the injected password may be deleted. The break-glass account is
the recovery path from then on.

Reversal is deliberately not an API: remove ``admin_retired`` from
``governance/settings/auth.yaml`` in the deploy repo and restart, and the engine
seeds the admin again from the store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.gitcrud.engine import ResourceNotFoundError

if TYPE_CHECKING:
    from dfe_engine.auth.accounts import AccountStore, DocuStoreAccountStore
    from dfe_engine.auth.groups import DocuStoreGroupStore, GroupStore
    from dfe_engine.gitcrud.engine import GitCrud
    from dfe_engine.gitops.repo import PublishResult

# The same governed settings doc the break-glass hash is committed to.
CLASS = "gov_settings"
NAME = "auth"
KEY = "admin_retired"

# The role that makes an account able to run the deployment without the bootstrap
# admin. Group roles resolve to it (dfe-admins), never the account directly.
ADMIN_ROLE = "admin"


def is_retired(crud: GitCrud | None) -> bool:
    """Whether the bootstrap admin has been retired. Fail-soft: unreadable reads false.

    False is the safe answer -- the deployment keeps its seeded admin -- so a
    hand-edited auth.yaml that no longer parses cannot lock an operator out.
    Without gitops there is no deploy repo to record the fact in, so no retirement.
    """
    if crud is None:
        return False
    try:
        value = crud.get(CLASS, NAME).get(KEY, False)
    except ResourceNotFoundError:
        return False
    except Exception as exc:
        logger.warning(
            "auth settings file is unreadable; the admin reads as not retired", error=str(exc)
        )
        return False
    return bool(value)


def set_retired(crud: GitCrud, actor: str) -> PublishResult:
    """Commit the retirement fact. Direct, never routed through a review PR.

    A fact sitting on an unmerged branch would let the next boot seed the admin
    again, so the write that the caller reports as done has to be on main.
    """
    message = build_message(
        CommitContext(ctype="rbac", scope=NAME, summary="retire the bootstrap admin", actor=actor)
    )
    return crud.set_key(CLASS, NAME, KEY, True, actor, message=message)


def another_admin_exists(
    account_store: AccountStore | DocuStoreAccountStore | None,
    group_store: GroupStore | DocuStoreGroupStore | None,
    admin_name: str,
) -> bool:
    """True when an enabled account other than the seeded pair carries the admin role.

    The precondition for retirement, and the hint the wizard enables its button
    from -- one predicate, so the UI cannot offer what the API refuses. Neither
    the bootstrap admin nor ``breakglass`` counts: retiring onto the recovery
    credential is what this exists to avoid. Only a system-scope group counts --
    an org-scoped group's roles bind inside that org, so its members cannot run
    the deployment.
    """
    from dfe_engine.auth.breakglass import USERNAME as BREAKGLASS_USERNAME
    from dfe_engine.auth.groups import GROUP_SCOPE_SYSTEM

    if account_store is None or group_store is None:
        return False
    admin_groups = {
        g.name
        for g in group_store.list()
        if ADMIN_ROLE in g.roles and g.scope == GROUP_SCOPE_SYSTEM
    }
    if not admin_groups:
        return False
    seeded = {admin_name, BREAKGLASS_USERNAME}
    return any(
        account.enabled and account.username not in seeded and admin_groups & set(account.groups)
        for account in account_store.list()
    )
