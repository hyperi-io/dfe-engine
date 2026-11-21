#  Project:      dfe-engine
#  File:         tests/unit/test_api/root_accounts_cases.py
#  Purpose:      Case tables for root accounts, which name no org and are bound to your org
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for root accounts, which name no org and are bound to your org."""

from typing import TypedDict


class SessionOrgCase(TypedDict):
    """An account naming *account_org* while *your_org* is named, and the org its sessions carry."""

    id: str
    account_org: str
    expected_org_id: str
    your_org: str | None


SESSION_ORG_CASES: list[SessionOrgCase] = [
    {
        "id": "a_root_account_is_bound_to_your_org",
        "account_org": "",
        "expected_org_id": "acme",
        "your_org": "acme",
    },
    {
        "id": "a_root_account_is_bound_to_none_while_no_org_is_yours",
        "account_org": "",
        "expected_org_id": "",
        "your_org": None,
    },
    {
        "id": "an_account_naming_an_org_keeps_it",
        "account_org": "globex",
        "expected_org_id": "globex",
        "your_org": "acme",
    },
]


class OrgFilterCase(TypedDict):
    """The accounts the list's `org` filter answers with while acme is your org."""

    id: str
    expected: set[str]
    org: str


ORG_FILTER_CASES: list[OrgFilterCase] = [
    {"id": "your_org_includes_root_accounts", "expected": {"acme-amy", "root-kim"}, "org": "acme"},
    {"id": "another_org_lists_only_its_own", "expected": {"globex-bob"}, "org": "globex"},
    {"id": "empty_lists_only_root_accounts", "expected": {"root-kim"}, "org": ""},
]


class OrgDisplayNameCase(TypedDict):
    """An account naming *account_org* while *your_org* is named, and the display name its read reports."""

    id: str
    account_org: str
    expected_org_display_name: str
    your_org: str | None


ORG_DISPLAY_NAME_CASES: list[OrgDisplayNameCase] = [
    {
        "id": "a_root_account_reads_your_orgs_display_name",
        "account_org": "",
        "expected_org_display_name": "Acme Corp",
        "your_org": "acme",
    },
    {
        "id": "a_bound_account_reads_its_orgs_display_name",
        "account_org": "globex",
        "expected_org_display_name": "Globex Inc",
        "your_org": "acme",
    },
    {
        "id": "an_org_without_a_display_name_reads_its_name",
        "account_org": "initech",
        "expected_org_display_name": "initech",
        "your_org": "acme",
    },
    {
        "id": "a_root_account_reads_empty_while_no_org_is_yours",
        "account_org": "",
        "expected_org_display_name": "",
        "your_org": None,
    },
]
