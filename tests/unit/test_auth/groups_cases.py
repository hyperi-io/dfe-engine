#  Project:      dfe-engine
#  File:         tests/unit/test_auth/groups_cases.py
#  Purpose:      Case tables for the auth.groups unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the auth.groups unit tests."""

from typing import TypedDict

from dfe_engine.auth.groups import Group
from tests.unit.test_auth.factories import make_group

_OKTA_ADMINS = make_group(name="okta-admins", source_id="00g-admins", source_provider="okta")
_SOC_TEAM = make_group(name="soc-team", source_id="SOC Team")
_VIEWERS = make_group(name="dfe-viewers")


class SourceIdHolderCase(TypedDict):
    id: str
    groups: list[Group]
    name: str
    source_id: str
    expected_holder: Group | None


SOURCE_ID_HOLDER_CASES: list[SourceIdHolderCase] = [
    {
        "id": "another_group_carries_it",
        "groups": [_SOC_TEAM, _VIEWERS],
        "name": "dfe-viewers",
        "source_id": "SOC Team",
        "expected_holder": _SOC_TEAM,
    },
    {
        "id": "another_groups_provider_does_not_matter",
        "groups": [_OKTA_ADMINS, _VIEWERS],
        "name": "dfe-viewers",
        "source_id": "00g-admins",
        "expected_holder": _OKTA_ADMINS,
    },
    {
        "id": "the_group_itself_carries_it",
        "groups": [_SOC_TEAM, _VIEWERS],
        "name": "soc-team",
        "source_id": "SOC Team",
        "expected_holder": None,
    },
    {
        "id": "no_group_carries_it",
        "groups": [_SOC_TEAM, _VIEWERS],
        "name": "dfe-viewers",
        "source_id": "dfe-viewers",
        "expected_holder": None,
    },
    {
        "id": "an_empty_id_is_never_held",
        "groups": [_SOC_TEAM, _VIEWERS],
        "name": "soc-team",
        "source_id": "",
        "expected_holder": None,
    },
]
