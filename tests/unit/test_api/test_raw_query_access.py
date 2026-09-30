#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_raw_query_access.py
#  Purpose:      Only the admin role reaches the raw query route
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST /api/v1/queries/raw runs caller SQL as the engine's own ClickHouse user.

A role holding ``query:execute`` or ``query:*`` reads through parameterised views
only; the raw route answers to ``raw_query:execute``, which no built-in role but
``admin`` holds. An unknown datasource is refused with 400 after the permission
check, so a 400 proves the caller got past it without a ClickHouse to query.
"""

import pytest

from dfe_engine.api.deps import create_access_token
from dfe_engine.auth.rbac_scopes import casbin_scopes_for_role_configuration, scopes_dict
from dfe_engine.auth.roles import RoleConfig

RAW = "/api/v1/queries/raw"
BODY = {"datasource": "nonexistent:default", "query": "SELECT currentUser()"}


def _session_holding(app, api_settings, role: str) -> dict[str, str]:
    """Headers for a fresh account whose only group grants *role* system-wide."""
    name = f"raw-probe-{role.replace('_', '-')}"
    app.state.group_store.create(f"{name}-group", roles=[role], members=[name])
    app.state.account_store.create(name, "raw-probe-password-2026", groups=[f"{name}-group"])
    token = create_access_token(data={"sub": name}, settings=api_settings)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "role", ["data_viewer", "data_analyst_viewer", "org_viewer", "data_analyst", "infra_admin"]
)
def test_a_role_below_admin_is_refused(client, app, api_settings, role):
    resp = client.post(RAW, json=BODY, headers=_session_holding(app, api_settings, role))

    assert resp.status_code == 403, resp.text
    assert "raw_query:execute" in resp.text


def test_admin_passes_the_permission_check(client, admin_headers):
    resp = client.post(RAW, json=BODY, headers=admin_headers)

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_datasource"


def test_a_view_role_still_executes_views(client, app, api_settings):
    """The view route keeps query:execute; only the raw route moved."""
    headers = _session_holding(app, api_settings, "data_viewer")

    resp = client.post("/api/v1/queries/views/analytics/test/execute", json={}, headers=headers)

    # No view executor in the unit app: 503 is past the permission check.
    assert resp.status_code == 503, resp.text


def test_only_the_admin_builtin_role_grants_the_action():
    config = RoleConfig.load_builtin()
    action = scopes_dict["raw_query_execute"]

    granting = [name for name in config.roles if config.has_permission(name, action)]

    assert action == "raw_query:execute"
    assert granting == ["admin"]


def test_the_action_is_in_the_role_editor_catalogue():
    assert "raw_query:execute" in casbin_scopes_for_role_configuration()
