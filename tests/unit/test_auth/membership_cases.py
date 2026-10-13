#  Project:      dfe-engine
#  File:         tests/unit/test_auth/membership_cases.py
#  Purpose:      Case tables for the auth.membership unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the auth.membership unit tests."""

from typing import TypedDict


class MembershipMirrorCase(TypedDict):
    id: str
    source_provider: str
    groups: list[str]
    group_name: str
    added: list[str]
    removed: list[str]
    expected_groups: list[str]


MEMBERSHIP_MIRROR_CASES: list[MembershipMirrorCase] = [
    {
        "id": "a_local_account_gains_the_group",
        "source_provider": "",
        "groups": ["dev"],
        "group_name": "ops",
        "added": ["alice"],
        "removed": [],
        "expected_groups": ["dev", "ops"],
    },
    {
        "id": "a_local_account_loses_the_group",
        "source_provider": "",
        "groups": ["ops", "dev"],
        "group_name": "ops",
        "added": [],
        "removed": ["alice"],
        "expected_groups": ["dev"],
    },
    {
        "id": "an_idp_account_joining_keeps_its_assertion",
        "source_provider": "entra",
        "groups": ["dfe-viewers"],
        "group_name": "DFE-Admins",
        "added": ["alice"],
        "removed": [],
        "expected_groups": ["dfe-viewers"],
    },
    {
        "id": "an_idp_account_leaving_keeps_its_assertion",
        "source_provider": "entra",
        "groups": ["DFE-Admins"],
        "group_name": "DFE-Admins",
        "added": [],
        "removed": ["alice"],
        "expected_groups": ["DFE-Admins"],
    },
    {
        "id": "a_scim_account_joining_keeps_its_list",
        "source_provider": "scim",
        "groups": [],
        "group_name": "scim-team",
        "added": ["alice"],
        "removed": [],
        "expected_groups": [],
    },
]
