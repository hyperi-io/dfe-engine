#  Project:      dfe-engine
#  File:         tests/support/org_groups.py
#  Purpose:      Seed an org-scoped group into the store, since the API creates only system-wide ones
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Seed an org-scoped group the way the org registry seeds an org's admin and viewer pair."""

from collections.abc import Iterable

from fastapi import FastAPI

from dfe_engine.auth.membership import sync_account_groups_for_membership_change


def seed_org_group(
    *,
    app: FastAPI,
    members: Iterable[str] = (),
    name: str,
    roles: list[str],
    scope: str,
    **fields: str,
) -> None:
    """Store group *name* at *scope* and mirror *members* onto their local accounts, as a membership write does."""
    held = list(members)
    app.state.group_store.create(name, members=held, roles=roles, scope=scope, **fields)
    sync_account_groups_for_membership_change(app.state.account_store, name, added=held)
