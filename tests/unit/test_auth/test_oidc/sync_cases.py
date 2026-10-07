#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/sync_cases.py
#  Purpose:      Case tables for the OIDC group sync tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the OIDC group sync tests."""

from typing import Any, TypedDict

from dfe_engine.auth.oidc.models import OIDCProvider
from tests.unit.test_auth.factories import make_oidc_provider


class HonouredLinkCase(TypedDict):
    id: str
    bindings: dict[str, str]
    source_provider: str
    expected_source_provider: str


HONOURED_LINK_CASES: list[HonouredLinkCase] = [
    {
        "id": "linked_for_any_provider_is_pinned_to_this_one",
        "bindings": {},
        "source_provider": "",
        "expected_source_provider": "test-sso",
    },
    {
        "id": "linked_for_this_provider",
        "bindings": {},
        "source_provider": "test-sso",
        "expected_source_provider": "test-sso",
    },
    {
        "id": "linked_for_a_stamp_bound_to_this_provider",
        "bindings": {"scim": "test-sso"},
        "source_provider": "scim",
        "expected_source_provider": "scim",
    },
]


class IdTakenCase(TypedDict):
    id: str
    bindings: dict[str, str]
    source_provider: str


ID_TAKEN_CASES: list[IdTakenCase] = [
    {"id": "linked_for_another_provider", "bindings": {}, "source_provider": "okta"},
    {"id": "linked_for_an_unbound_stamp", "bindings": {}, "source_provider": "scim"},
    {
        "id": "linked_for_a_stamp_bound_to_another_provider",
        "bindings": {"scim": "entra"},
        "source_provider": "scim",
    },
]


class NonApiModeCase(TypedDict):
    id: str
    provider: OIDCProvider
    expected_result: dict[str, Any]


NON_API_MODE_CASES: list[NonApiModeCase] = [
    {
        "id": "manual",
        "provider": make_oidc_provider(groups={"mode": "manual"}),
        "expected_result": {
            "created": 0,
            "error": None,
            "groups_skipped": 0,
            "skipped": "Groups mode is 'manual': group membership is managed in DFE, so there is "
            "no directory to sync",
            "total": 0,
            "updated": 0,
        },
    },
    {
        "id": "token_claim",
        "provider": make_oidc_provider(groups={"mode": "token_claim"}, type="okta"),
        "expected_result": {
            "created": 0,
            "error": None,
            "groups_skipped": 0,
            "skipped": "Groups mode is 'token_claim': groups come from each login's token, so "
            "there is no directory to sync; link a group to the IdP group by its source ID",
            "total": 0,
            "updated": 0,
        },
    },
]
