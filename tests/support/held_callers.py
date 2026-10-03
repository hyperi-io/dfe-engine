#  Project:      dfe-engine
#  File:         tests/support/held_callers.py
#  Purpose:      Build a caller from a group and a role definition, as a deployment does
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A caller whose roles and orgs come from one group, and the roles it may hold.

``define_role`` writes the definitions ``authorize()`` reads, which is where the
roles API leaves a create or update. ``caller_in_group`` makes an account in one
group and returns a client carrying its token, so its grants and orgs resolve
live from the group on every request.
"""

from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.auth.roles import RoleDefinition


def define_role(app, name: str, permissions: list[str], *, scoped: bool) -> None:
    """Add ``name`` to the role definitions the app authorises against."""
    app.state.role_config.roles[name] = RoleDefinition(
        description=f"{name} for tests", permissions=permissions, scoped=scoped
    )


def caller_in_group(
    app,
    settings,
    name: str,
    *,
    roles: list[str],
    org_ids: list[str],
    scope: str = "system",
) -> TestClient:
    """A client for a new account whose one group holds ``roles`` and ``org_ids`` at ``scope``."""
    group = f"{name}-group"
    app.state.group_store.create(group, roles=roles, scope=scope)
    app.state.group_store.update(group, org_ids=org_ids)
    app.state.account_store.create(name, f"{name}-password-2026", groups=[group])
    app.state.group_store.add_member(group, name)
    token = create_access_token(data={"sub": name}, settings=settings)
    caller = TestClient(app, raise_server_exceptions=False)
    caller.headers.update({"Authorization": f"Bearer {token}"})
    return caller
