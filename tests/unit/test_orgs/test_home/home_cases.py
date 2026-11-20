#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_home/home_cases.py
#  Purpose:      Cases for the your-org store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Cases for the your-org store."""

from typing import TypedDict


class HomeOrgStoreCase(TypedDict):
    id: str
    doc: str
    expected_name: str | None


class HomeOrgStoreRejectCase(TypedDict):
    id: str
    doc: str
    message: str


HOME_ORG_STORE_CASES: list[HomeOrgStoreCase] = [
    {"id": "empty_doc", "doc": "", "expected_name": None},
    {"id": "no_name_key", "doc": "other: acme\n", "expected_name": None},
    {"id": "null_name", "doc": "name: null\n", "expected_name": None},
    {"id": "named", "doc": "name: acme\n", "expected_name": "acme"},
    {"id": "named_default", "doc": "name: default\n", "expected_name": "default"},
]

HOME_ORG_STORE_REJECT_CASES: list[HomeOrgStoreRejectCase] = [
    {
        "id": "a_list",
        "doc": "- acme\n- beta\n",
        "message": "The your-organisation doc is ['acme', 'beta'], not a mapping",
    },
    {
        "id": "blank_name",
        "doc": 'name: ""\n',
        "message": "The your-organisation doc names '', not an org",
    },
    {
        "id": "a_number",
        "doc": "name: 3\n",
        "message": "The your-organisation doc names 3, not an org",
    },
    {
        "id": "a_mapping",
        "doc": "name:\n  org: acme\n",
        "message": "The your-organisation doc names {'org': 'acme'}, not an org",
    },
]
