#  Project:      dfe-engine
#  File:         tests/unit/test_auth/membership_cases.py
#  Purpose:      Case tables for the auth.membership unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the auth.membership unit tests."""

from typing import TypedDict

from dfe_engine.auth.accounts import Account
from dfe_engine.auth.groups import Group
from tests.unit.test_auth.factories import make_account, make_group, make_linked_identity


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


class GroupsHeldLinkedCase(TypedDict):
    id: str
    account: Account
    bindings: dict[str, str]
    expected_groups: list[str]


LINKED_SIGN_IN_GROUPS: list[Group] = [
    make_group(name="entra-admins", roles=["admin"], source_id="g-entra", source_provider="entra"),
    make_group(
        name="okta-analysts", roles=["data_analyst"], source_id="g-okta", source_provider="okta"
    ),
    make_group(name="scim-viewers", roles=["viewer"], source_id="g-scim", source_provider="scim"),
]

GROUPS_HELD_LINKED_CASES: list[GroupsHeldLinkedCase] = [
    {
        "id": "a_local_person_holds_its_linked_sign_ins_groups",
        "account": make_account(
            identities=[
                make_linked_identity(groups=["g-okta"], key="00u1", provider="okta", subject="00u1")
            ],
            username="kaz",
        ),
        "bindings": {},
        "expected_groups": ["okta-analysts"],
    },
    {
        "id": "a_linked_assertion_links_only_under_its_own_provider",
        "account": make_account(
            identities=[
                make_linked_identity(
                    groups=["g-entra"], key="00u1", provider="okta", subject="00u1"
                )
            ],
            username="kaz",
        ),
        "bindings": {},
        "expected_groups": [],
    },
    {
        "id": "a_local_persons_own_list_still_holds_nothing",
        "account": make_account(
            groups=["g-okta"],
            identities=[make_linked_identity(key="00u1", provider="okta", subject="00u1")],
            username="kaz",
        ),
        "bindings": {},
        "expected_groups": [],
    },
    {
        "id": "a_binding_joins_a_linked_sign_in_to_its_stamp",
        "account": make_account(
            identities=[
                make_linked_identity(
                    groups=["g-scim"], key="oid-1", provider="entra", subject="oid-1"
                )
            ],
            username="kaz",
        ),
        "bindings": {"scim": "entra"},
        "expected_groups": ["scim-viewers"],
    },
    {
        "id": "an_idp_person_holds_its_own_and_its_linked_assertions",
        "account": make_account(
            external=True,
            groups=["g-entra"],
            identities=[
                make_linked_identity(groups=["g-okta"], key="00u1", provider="okta", subject="00u1")
            ],
            source_provider="entra",
            username="jane-corp-com",
        ),
        "bindings": {},
        "expected_groups": ["entra-admins", "okta-analysts"],
    },
]
