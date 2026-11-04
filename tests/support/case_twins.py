#  Project:      dfe-engine
#  File:         tests/support/case_twins.py
#  Purpose:      Store a group whose name another group holds in another case
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A stored group that ``GroupStore.create`` would refuse as a case twin.

A deploy repo can still deliver one as a file, so the routes that read groups
are tested against it.
"""

from dfe_engine.auth.groups import Group, GroupStore


def store_case_twin_group(store: GroupStore, *, name: str, members: list[str]) -> None:
    """Write group *name* with no roles, past create's case-twin refusal.

    Args:
        store: The group store to write into.
        name: The group's name, which may differ from a stored one only in case.
        members: The group's member usernames.
    """
    store._write(Group(members=members, name=name, roles=[]))
