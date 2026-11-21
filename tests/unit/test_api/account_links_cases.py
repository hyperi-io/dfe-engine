#  Project:      dfe-engine
#  File:         tests/unit/test_api/account_links_cases.py
#  Purpose:      Case tables for the API tests of accounts that IdP sign-ins are linked to
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the API tests of accounts that IdP sign-ins are linked to."""

from typing import TypedDict

from tests.unit.test_auth.factories import make_linked_identity


class ProviderTypesCase(TypedDict):
    """Updates to the local kaz, linked to the generic provider stub, and the types it then reads with."""

    id: str
    expected_identity_types: list[str]
    expected_oidc_type: str
    updates: dict[str, object]


PROVIDER_TYPES_CASES: list[ProviderTypesCase] = [
    {
        "id": "a_linked_sign_in_reads_its_provider_type",
        "expected_identity_types": ["generic"],
        "expected_oidc_type": "",
        "updates": {},
    },
    {
        "id": "an_idp_account_reads_its_provider_type",
        "expected_identity_types": [],
        "expected_oidc_type": "generic",
        "updates": {"external": True, "identities": [], "oidc_id": "stub"},
    },
    {
        "id": "an_unregistered_provider_reads_empty",
        "expected_identity_types": [""],
        "expected_oidc_type": "",
        "updates": {
            "external": True,
            "identities": [
                make_linked_identity(key="gone-kaz", provider="gone", subject="gone-kaz")
            ],
            "oidc_id": "gone",
        },
    },
]
