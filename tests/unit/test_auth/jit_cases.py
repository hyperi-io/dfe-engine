#  Project:      dfe-engine
#  File:         tests/unit/test_auth/jit_cases.py
#  Purpose:      Case tables for linking a first IdP login to an account by its email
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for linking a first IdP login to an account by its email."""

from typing import Any, TypedDict

from dfe_engine.auth.accounts import Account
from dfe_engine.auth.jit import (
    JitAccountUnavailableError,
    JitEmailHeldError,
    JitEmailUntrustedError,
    JitLinkRequiredError,
    JitNoRoleError,
)
from dfe_engine.auth.models import AuthenticationError
from tests.unit.test_auth.factories import make_account

ENTRA_SUBJECT = "oid-0001"
"""The entra login every row makes. It sanitises to itself, so it is also the sign-in's key."""

JANE = "jane@corp.com"

LINK_REQUIRED_MESSAGE = "Sign in with this account's password to link your identity provider to it"

EMAIL_UNTRUSTED_MESSAGE = (
    "OIDC login refused: this identity provider did not verify an email in a domain it "
    "is trusted for. Ask a DFE administrator to trust your email domain on it."
)

EMAIL_HELD_MESSAGE = (
    "OIDC login refused: another DFE account already uses this email. Ask a DFE "
    "administrator to trust your email domain on this identity provider."
)

NO_ROLE_MESSAGE = (
    "OIDC login refused: no DFE group with a role is linked to your identity provider "
    "groups. Ask a DFE administrator to link one."
)


def _local(*, email: str = JANE, username: str = "kaz", **kwargs: Any) -> Account:
    """A local account holding *email*."""
    return make_account(email=email, username=username, **kwargs)


def _okta(
    *, email: str = JANE, email_verified: bool = True, username: str = "jane-okta", **kwargs: Any
) -> Account:
    """An account an okta login created, holding *email*."""
    return make_account(
        email=email,
        email_verified=email_verified,
        external=True,
        source_provider="okta",
        subject="00u1",
        username=username,
        **kwargs,
    )


class EnsureAccountLinkCase(TypedDict):
    id: str
    email: str
    email_verified: bool | None
    expected_identity_keys: list[str]
    expected_linked_to: str
    expected_username: str
    stored: list[Account]


ENSURE_ACCOUNT_LINK_CASES: list[EnsureAccountLinkCase] = [
    {
        "id": "an_idp_account_links_at_once",
        "email": JANE,
        "email_verified": True,
        "expected_identity_keys": [ENTRA_SUBJECT],
        "expected_linked_to": "jane-okta",
        "expected_username": "jane-okta",
        "stored": [_okta()],
    },
    {
        "id": "emails_match_ignoring_case",
        "email": JANE,
        "email_verified": True,
        "expected_identity_keys": [ENTRA_SUBJECT],
        "expected_linked_to": "jane-okta",
        "expected_username": "jane-okta",
        "stored": [_okta(email=" Jane@Corp.COM")],
    },
    {
        "id": "a_proxied_login_never_links",
        "email": JANE,
        "email_verified": None,
        "expected_identity_keys": [],
        "expected_linked_to": "",
        "expected_username": ENTRA_SUBJECT,
        "stored": [_okta()],
    },
    {
        "id": "a_recovery_credential_is_no_match",
        "email": JANE,
        "email_verified": True,
        "expected_identity_keys": [],
        "expected_linked_to": "",
        "expected_username": ENTRA_SUBJECT,
        "stored": [_local(groups=["dfe-admins"], username="admin")],
    },
    {
        "id": "a_soft_deleted_account_is_no_match",
        "email": JANE,
        "email_verified": True,
        "expected_identity_keys": [],
        "expected_linked_to": "",
        "expected_username": ENTRA_SUBJECT,
        "stored": [_local(previous_username="kaz", username="0b9f-deleted")],
    },
    {
        "id": "a_self_set_local_email_is_no_match",
        "email": JANE,
        "email_verified": True,
        "expected_identity_keys": [],
        "expected_linked_to": "",
        "expected_username": ENTRA_SUBJECT,
        "stored": [_local(email_verified=False)],
    },
    {
        "id": "a_link_record_is_no_match",
        "email": JANE,
        "email_verified": True,
        "expected_identity_keys": [],
        "expected_linked_to": "",
        "expected_username": ENTRA_SUBJECT,
        "stored": [_local(linked_to="someone", username="00u9")],
    },
]


class EnsureAccountLinkRefusedCase(TypedDict):
    id: str
    email_verified: bool
    expected_error: type[AuthenticationError]
    expected_message: str
    oidc_groups: list[str]
    stored: list[Account]


ENSURE_ACCOUNT_LINK_REFUSED_CASES: list[EnsureAccountLinkRefusedCase] = [
    {
        "email_verified": True,
        "id": "a_local_account_needs_its_password",
        "expected_error": JitLinkRequiredError,
        "expected_message": LINK_REQUIRED_MESSAGE,
        "oidc_groups": ["dfe-analysts"],
        "stored": [_local()],
    },
    {
        "email_verified": True,
        "id": "a_disabled_match_refuses",
        "expected_error": JitAccountUnavailableError,
        "expected_message": "Account disabled",
        "oidc_groups": ["dfe-analysts"],
        "stored": [_local(enabled=False)],
    },
    {
        "email_verified": True,
        "id": "a_blocked_match_refuses",
        "expected_error": JitAccountUnavailableError,
        "expected_message": "Account blocked",
        "oidc_groups": ["dfe-analysts"],
        "stored": [_okta(blocked=True)],
    },
    {
        "email_verified": True,
        "id": "a_match_left_holding_no_role_refuses",
        "expected_error": JitNoRoleError,
        "expected_message": NO_ROLE_MESSAGE,
        "oidc_groups": ["unlinked-team"],
        "stored": [_local()],
    },
    {
        "email_verified": True,
        "id": "two_vouched_holders_refuse",
        "expected_error": JitEmailHeldError,
        "expected_message": EMAIL_HELD_MESSAGE,
        "oidc_groups": ["dfe-analysts"],
        "stored": [_local(), _local(username="kaz2")],
    },
    {
        "email_verified": False,
        "id": "an_untrusted_email_another_account_holds_refuses",
        "expected_error": JitEmailUntrustedError,
        "expected_message": EMAIL_UNTRUSTED_MESSAGE,
        "oidc_groups": ["dfe-analysts"],
        "stored": [_local()],
    },
    {
        "email_verified": False,
        "id": "an_untrusted_email_nobody_holds_refuses",
        "expected_error": JitEmailUntrustedError,
        "expected_message": EMAIL_UNTRUSTED_MESSAGE,
        "oidc_groups": ["dfe-analysts"],
        "stored": [],
    },
    {
        "email_verified": True,
        "id": "an_unvouched_idp_account_holding_it_refuses",
        "expected_error": JitEmailHeldError,
        "expected_message": EMAIL_HELD_MESSAGE,
        "oidc_groups": ["dfe-analysts"],
        "stored": [_okta(email_verified=False)],
    },
]


class CompleteLinkStaleCase(TypedDict):
    """A store that changed after a ticket naming the local account kaz was issued."""

    id: str
    stored: list[Account]


COMPLETE_LINK_STALE_CASES: list[CompleteLinkStaleCase] = [
    {"id": "the_account_changed_its_email", "stored": [_local(email="kaz@other.example")]},
    {"id": "a_second_account_holds_the_email", "stored": [_local(), _local(username="kaz2")]},
    {
        "id": "an_idp_now_owns_the_account",
        "stored": [_okta(username="kaz")],
    },
    {
        "id": "the_sign_in_has_its_own_account",
        "stored": [
            _local(),
            make_account(
                external=True,
                source_provider="entra",
                subject=ENTRA_SUBJECT,
                username=ENTRA_SUBJECT,
            ),
        ],
    },
]
