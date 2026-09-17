#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/rp_cases.py
#  Purpose:      Case tables for OIDC relying-party claim handling
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for OIDC relying-party claim handling."""

from __future__ import annotations

import json
from typing import Any, TypedDict

from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.rp import NormalizedIdentity
from tests.unit.test_auth.factories import make_normalized_identity, make_oidc_provider

OKTA_SUBJECT = "00u15mxs3ecygt7oj698"

OKTA_USERINFO = {
    "sub": OKTA_SUBJECT,
    "email": "jane@corp.com",
    "groups": ["dfe-admins"],
    "name": "Jane Citizen",
    "preferred_username": "jane@corp.com",
}


class ExtractIdentityCase(TypedDict):
    id: str
    claims: dict[str, Any]
    expected_identity: NormalizedIdentity
    provider: OIDCProvider


class FillFromUserinfoEndpointCase(TypedDict):
    id: str
    advertise_userinfo: bool
    expected_claims: dict[str, Any]
    id_token_claims: dict[str, Any]
    token: dict[str, Any]
    userinfo_body: str
    userinfo_status: int


class MergeUserinfoClaimsCase(TypedDict):
    id: str
    expected_claims: dict[str, Any]
    id_token_claims: dict[str, Any]
    userinfo_claims: dict[str, Any]


class MergeUserinfoClaimsSubjectMismatchCase(TypedDict):
    id: str
    id_token_claims: dict[str, Any]
    userinfo_claims: dict[str, Any]


EXTRACT_IDENTITY_CASES: list[ExtractIdentityCase] = [
    {
        "id": "generic_default_groups_claim",
        "claims": {
            "sub": "alice@example.com",
            "aud": "dfe",
            "email": "alice@example.com",
            "groups": ["soc", "admins"],
        },
        "expected_identity": make_normalized_identity(
            email="alice@example.com", groups=["soc", "admins"], subject="alice@example.com"
        ),
        "provider": make_oidc_provider(),
    },
    {
        "id": "okta_default_groups_claim",
        "claims": {
            "sub": "00u1abc",
            "email": "bob@acme.com",
            "groups": ["Everyone", "dfe-analysts"],
        },
        "expected_identity": make_normalized_identity(
            email="bob@acme.com", groups=["Everyone", "dfe-analysts"], subject="00u1abc"
        ),
        "provider": make_oidc_provider(issuer="https://acme.okta.com", type="okta"),
    },
    {
        "id": "custom_groups_claim_name",
        "claims": {"sub": "guid-123", "email": "carol@acme.com", "roles": ["group-guid-a"]},
        "expected_identity": make_normalized_identity(
            email="carol@acme.com", groups=["group-guid-a"], subject="guid-123"
        ),
        "provider": make_oidc_provider(
            groups={"claim_name": "roles", "mode": "token_claim"},
            issuer="https://login.microsoftonline.com/tid/v2.0",
            type="entra_id",
        ),
    },
    {
        "id": "comma_separated_groups_string",
        "claims": {"sub": "x", "email": "x@y.z", "groups": " soc , admins "},
        "expected_identity": make_normalized_identity(
            email="x@y.z", groups=["soc", "admins"], subject="x"
        ),
        "provider": make_oidc_provider(),
    },
    {
        "id": "only_sub_yields_defaults",
        "claims": {"sub": "only-sub"},
        "expected_identity": make_normalized_identity(subject="only-sub"),
        "provider": make_oidc_provider(),
    },
    {
        "id": "entra_group_overage_marker",
        "claims": {
            "sub": "pairwise-sub",
            "_claim_names": {"groups": "src1"},
            "_claim_sources": {
                "src1": {"endpoint": "https://graph.microsoft.com/v1.0/users/oid/getMemberObjects"}
            },
            "email": "big@acme.com",
            "oid": "00000000-user-oid",
        },
        "expected_identity": make_normalized_identity(
            email="big@acme.com", groups_overflowed=True, subject="pairwise-sub"
        ),
        "provider": make_oidc_provider(
            issuer="https://login.microsoftonline.com/tid/v2.0", type="entra_id"
        ),
    },
    {
        "id": "entra_groups_array_is_not_overage",
        "claims": {"sub": "s", "email": "a@b.c", "groups": ["guid-a", "guid-b"]},
        "expected_identity": make_normalized_identity(
            email="a@b.c", groups=["guid-a", "guid-b"], subject="s"
        ),
        "provider": make_oidc_provider(
            issuer="https://login.microsoftonline.com/tid/v2.0", type="entra_id"
        ),
    },
    {
        "id": "name_claim_wins_over_preferred_username",
        "claims": OKTA_USERINFO,
        "expected_identity": make_normalized_identity(
            email="jane@corp.com", groups=["dfe-admins"], name="Jane Citizen", subject=OKTA_SUBJECT
        ),
        "provider": make_oidc_provider(issuer="https://acme.okta.com", type="okta"),
    },
    {
        "id": "preferred_username_when_no_name",
        "claims": {"sub": "XlZ_SJfeaMSC9HM8", "preferred_username": "dfe-admin@ms.hyperi.io"},
        "expected_identity": make_normalized_identity(
            email="dfe-admin@ms.hyperi.io",
            name="dfe-admin@ms.hyperi.io",
            subject="XlZ_SJfeaMSC9HM8",
        ),
        "provider": make_oidc_provider(
            issuer="https://login.microsoftonline.com/tid/v2.0", type="entra_id"
        ),
    },
    {
        "id": "empty_name_falls_back_to_preferred_username",
        "claims": {"sub": "s", "name": "", "preferred_username": "jane"},
        "expected_identity": make_normalized_identity(email="jane", name="jane", subject="s"),
        "provider": make_oidc_provider(),
    },
    {
        "id": "whitespace_name_falls_back_to_preferred_username",
        "claims": {"sub": "s", "name": "   ", "preferred_username": " jane "},
        "expected_identity": make_normalized_identity(email=" jane ", name="jane", subject="s"),
        "provider": make_oidc_provider(),
    },
    {
        "id": "non_string_name_is_ignored",
        "claims": {"sub": "s", "name": {"given": "Jane"}, "preferred_username": "jane"},
        "expected_identity": make_normalized_identity(email="jane", name="jane", subject="s"),
        "provider": make_oidc_provider(),
    },
    {
        "id": "email_falls_back_to_upn_without_preferred_username",
        "claims": {"sub": "guid-123", "upn": "dfe-admin@ms.hyperi.io"},
        "expected_identity": make_normalized_identity(
            email="dfe-admin@ms.hyperi.io", subject="guid-123"
        ),
        "provider": make_oidc_provider(
            issuer="https://login.microsoftonline.com/tid/v2.0", type="entra_id"
        ),
    },
    {
        "id": "email_claim_wins_over_fallbacks",
        "claims": {
            "sub": "guid-123",
            "email": "real@acme.com",
            "preferred_username": "upn@ms.hyperi.io",
            "upn": "upn@ms.hyperi.io",
        },
        "expected_identity": make_normalized_identity(
            email="real@acme.com", name="upn@ms.hyperi.io", subject="guid-123"
        ),
        "provider": make_oidc_provider(
            issuer="https://login.microsoftonline.com/tid/v2.0", type="entra_id"
        ),
    },
    {
        "id": "empty_email_claim_falls_through",
        "claims": {"sub": "guid-123", "email": "", "preferred_username": "dfe-admin@ms.hyperi.io"},
        "expected_identity": make_normalized_identity(
            email="dfe-admin@ms.hyperi.io", name="dfe-admin@ms.hyperi.io", subject="guid-123"
        ),
        "provider": make_oidc_provider(
            issuer="https://login.microsoftonline.com/tid/v2.0", type="entra_id"
        ),
    },
]

FILL_FROM_USERINFO_ENDPOINT_CASES: list[FillFromUserinfoEndpointCase] = [
    {
        "id": "fills_profile_claims_but_not_groups",
        "advertise_userinfo": True,
        "expected_claims": {
            "sub": OKTA_SUBJECT,
            "aud": "dfe",
            "email": "jane@corp.com",
            "name": "Jane Citizen",
            "preferred_username": "jane@corp.com",
        },
        "id_token_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "token": {"access_token": "at", "token_type": "Bearer"},
        "userinfo_body": json.dumps(OKTA_USERINFO),
        "userinfo_status": 200,
    },
    {
        "id": "no_userinfo_endpoint_advertised",
        "advertise_userinfo": False,
        "expected_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "id_token_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "token": {"access_token": "at", "token_type": "Bearer"},
        "userinfo_body": json.dumps(OKTA_USERINFO),
        "userinfo_status": 200,
    },
    {
        "id": "userinfo_server_error",
        "advertise_userinfo": True,
        "expected_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "id_token_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "token": {"access_token": "at", "token_type": "Bearer"},
        "userinfo_body": json.dumps(OKTA_USERINFO),
        "userinfo_status": 500,
    },
    {
        "id": "userinfo_body_not_json",
        "advertise_userinfo": True,
        "expected_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "id_token_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "token": {"access_token": "at", "token_type": "Bearer"},
        "userinfo_body": "<html>sign in</html>",
        "userinfo_status": 200,
    },
    {
        "id": "userinfo_for_another_subject",
        "advertise_userinfo": True,
        "expected_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "id_token_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "token": {"access_token": "at", "token_type": "Bearer"},
        "userinfo_body": json.dumps({**OKTA_USERINFO, "sub": "00uattacker"}),
        "userinfo_status": 200,
    },
    {
        "id": "access_token_expired_without_refresh_token",
        "advertise_userinfo": True,
        "expected_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "id_token_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "token": {"access_token": "at", "expires_at": 1, "token_type": "Bearer"},
        "userinfo_body": json.dumps(OKTA_USERINFO),
        "userinfo_status": 200,
    },
]

MERGE_USERINFO_CLAIMS_CASES: list[MergeUserinfoClaimsCase] = [
    {
        "id": "thin_id_token_gets_profile_claims_only",
        "expected_claims": {
            "sub": OKTA_SUBJECT,
            "aud": "dfe",
            "email": "jane@corp.com",
            "name": "Jane Citizen",
            "preferred_username": "jane@corp.com",
        },
        "id_token_claims": {"sub": OKTA_SUBJECT, "aud": "dfe"},
        "userinfo_claims": OKTA_USERINFO,
    },
    {
        "id": "id_token_value_wins_on_conflict",
        "expected_claims": {"sub": "s", "email": "signed@corp.com", "name": "Jane Citizen"},
        "id_token_claims": {"sub": "s", "email": "signed@corp.com"},
        "userinfo_claims": {"sub": "s", "email": "other@corp.com", "name": "Jane Citizen"},
    },
    {
        "id": "blank_id_token_claim_is_filled",
        "expected_claims": {"sub": "s", "name": "Jane Citizen"},
        "id_token_claims": {"sub": "s", "name": "  "},
        "userinfo_claims": {"sub": "s", "name": "Jane Citizen"},
    },
    {
        "id": "non_string_userinfo_claim_is_ignored",
        "expected_claims": {"sub": "s", "preferred_username": "jane"},
        "id_token_claims": {"sub": "s"},
        "userinfo_claims": {"sub": "s", "name": {"given": "Jane"}, "preferred_username": "jane"},
    },
    {
        "id": "userinfo_with_only_sub_changes_nothing",
        "expected_claims": {"sub": "s", "aud": "dfe"},
        "id_token_claims": {"sub": "s", "aud": "dfe"},
        "userinfo_claims": {"sub": "s"},
    },
]

MERGE_USERINFO_CLAIMS_SUBJECT_MISMATCH_CASES: list[MergeUserinfoClaimsSubjectMismatchCase] = [
    {
        "id": "different_subject",
        "id_token_claims": {"sub": OKTA_SUBJECT},
        "userinfo_claims": {"sub": "00uattacker", "name": "Someone Else"},
    },
    {
        "id": "userinfo_without_subject",
        "id_token_claims": {"sub": OKTA_SUBJECT},
        "userinfo_claims": {"name": "Jane Citizen"},
    },
]
