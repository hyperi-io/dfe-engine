#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/sync_cases.py
#  Purpose:      Case tables for the OIDC group sync tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the OIDC group sync tests."""

from typing import TypedDict


class SyncIdentifierCollisionCase(TypedDict):
    id: str
    stored_name: str
    stored_link: dict[str, str]
    group_id: str
    group_name: str


# Each provider group would be created as soc-team; the stored group holds its name or id in the other namespace.
SYNC_IDENTIFIER_COLLISION_CASES: list[SyncIdentifierCollisionCase] = [
    {
        "id": "the_name_is_a_stored_groups_source_id",
        "stored_name": "ops",
        "stored_link": {"source_id": "soc-team", "source_provider": "keycloak"},
        "group_id": "g-soc",
        "group_name": "SOC Team",
    },
    {
        "id": "the_id_is_a_stored_groups_name",
        "stored_name": "g-soc",
        "stored_link": {},
        "group_id": "g-soc",
        "group_name": "SOC Team",
    },
]
