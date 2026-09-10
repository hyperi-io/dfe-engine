#  Project:      dfe-engine
#  File:         auth/breakglass.py
#  Purpose:      The break-glass recovery account and its hash in governance settings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The break-glass account: an OIDC-bypassing recovery admin verified from gitcrud.

The everyday ``admin`` account is reconciled from injected config on every boot,
so it is only as durable as the secret store that injects it. ``breakglass`` is
the recovery credential for the case where that store, the IdP, or the engine's
own state is gone: its bcrypt hash is committed to the deploy repo's governance
settings (``governance/settings/auth.yaml``), which survives a total teardown of
the engine and the UI.

The plaintext is never stored. ``DFE_AUTH_BREAKGLASS_PASSWORD`` is a mint-once
input: with no hash committed yet it is hashed and committed, and from then on it
is ignored, so leaving it in a compose file does not reassert an old password.

``breakglass.enabled`` (default true) turns the account off once OIDC is verified;
a login attempt against a disabled account is refused with a reason rather than
the generic invalid-credentials answer, because the caller is an operator who
needs to know the account was switched off, not that they mistyped.

With gitops disabled there is nowhere durable to hold the hash, so no break-glass
account is seeded at all -- the deployment has only ``admin``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.auth.accounts import Account, hash_password
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.gitcrud.engine import ResourceNotFoundError

if TYPE_CHECKING:
    from dfe_engine.auth.accounts import AccountStore, DocuStoreAccountStore
    from dfe_engine.auth.groups import DocuStoreGroupStore, GroupStore
    from dfe_engine.gitcrud.engine import GitCrud

# The break-glass username is fixed: a deployment that has lost its UI recovers by
# typing a name the operator can be told once, in the docs.
USERNAME = "breakglass"
# Admin group, so the account resolves the admin role like any other member.
GROUP = "dfe-admins"

# Where the hash lives: the same governed settings class the auto-merge flag uses.
CLASS = "gov_settings"
NAME = "auth"
KEY_HASH = "breakglass.password_hash"
KEY_ENABLED = "breakglass.enabled"


def _settings_doc(crud: GitCrud) -> dict:
    """The governance auth settings doc; absent or unreadable reads as empty.

    Fail-soft on purpose: a hand-edited auth.yaml that no longer parses must not
    500 every login and every startup. An empty doc means no stored hash (no
    break-glass account) and the enabled default.
    """
    try:
        return crud.get(CLASS, NAME)
    except ResourceNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("break-glass settings file is unreadable", error=str(exc))
        return {}


def _section(crud: GitCrud) -> dict:
    section = _settings_doc(crud).get("breakglass")
    return section if isinstance(section, dict) else {}


def _remote_hash(crud: GitCrud) -> str:
    """The hash the REMOTE deploy repo carries right now, or empty when it carries none.

    Fail-soft for the same reason :func:`_settings_doc` is: an unreachable remote must
    not stop a boot, and reading it as absent only costs a mint.
    """
    try:
        section = crud.get_remote(CLASS, NAME).get("breakglass")
    except Exception as exc:
        logger.warning("Could not read the break-glass hash from the remote", error=str(exc))
        return ""
    value = section.get("password_hash", "") if isinstance(section, dict) else ""
    return value if isinstance(value, str) else ""


def stored_hash(crud: GitCrud | None) -> str:
    """The committed bcrypt hash, or empty when there is none."""
    if crud is None:
        return ""
    value = _section(crud).get("password_hash", "")
    return value if isinstance(value, str) else ""


def is_enabled(crud: GitCrud | None) -> bool:
    """Whether the break-glass account may authenticate. Default and fallback: true.

    An unreadable or absent setting leaves the recovery path open -- the account
    still needs its password, and failing closed here would remove the very escape
    hatch this account exists to be.
    """
    if crud is None:
        return True
    return bool(_section(crud).get("enabled", True))


def set_enabled(crud: GitCrud, enabled: bool, actor: str) -> None:
    """Commit the enabled flag through the governed path (audited)."""
    message = build_message(
        CommitContext(
            ctype="rbac",
            scope=NAME,
            summary=f"break-glass {'enabled' if enabled else 'disabled'}",
            actor=actor,
        )
    )
    crud.set_key(CLASS, NAME, KEY_ENABLED, enabled, actor, message=message)


def mint_hash(crud: GitCrud, password: str, actor: str = "dfe-engine") -> str:
    """Commit a hash of *password* as the break-glass hash, unless one is already there.

    Replicas boot together and each reads its own clone, which on a first boot carries
    no hash yet, so the deploy repo is read before minting and after committing -- one
    deployment must end up with ONE break-glass credential, not one per replica.

    Returns:
        The hash the deploy repo holds for the account.
    """
    committed = _remote_hash(crud)
    if committed:
        return committed
    digest = hash_password(password)
    message = build_message(
        CommitContext(ctype="rbac", scope=NAME, summary="mint break-glass hash", actor=actor)
    )
    crud.set_key(CLASS, NAME, KEY_HASH, digest, actor, message=message)
    return _remote_hash(crud) or digest


def seed(
    account_store: AccountStore | DocuStoreAccountStore,
    group_store: GroupStore | DocuStoreGroupStore,
    crud: GitCrud | None,
    mint_password: str = "",
) -> str:
    """Reconcile the break-glass account against the committed hash. Returns the hash.

    Mints the hash first when none is committed and a first-boot password was
    configured. With no gitcrud and no committed hash there is nothing durable to
    seed from, so no account is created and an empty string comes back.
    """
    if crud is None:
        return ""

    digest = stored_hash(crud)
    if not digest and mint_password:
        digest = mint_hash(crud, mint_password)
        logger.info("Minted the break-glass password hash into the deploy repo")
    if not digest:
        return ""

    account = account_store.get(USERNAME)
    if account is None or account.password_hash != digest:
        # enabled stays True on the account: the governance flag is the ONE switch.
        now = datetime.now(UTC).isoformat()
        account_store.put(
            Account(
                username=USERNAME,
                password_hash=digest,
                enabled=True,
                groups=[GROUP],
                created_at=account.created_at if account else now,
                updated_at=now,
            )
        )
        logger.info("Reconciled the break-glass account from the deploy repo hash")
    group_store.add_member(GROUP, USERNAME)
    return digest
