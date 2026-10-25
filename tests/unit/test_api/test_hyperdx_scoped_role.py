#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hyperdx_scoped_role.py
#  Purpose:      A scoped role holding query:execute gets its org's connection, never the platform's
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``GET /hyperdx/connection`` hands a scoped role its org's pinned ClickHouse user.

The platform reader reads every org's rows, so it goes only to a caller whose
``query:execute`` comes from a declared, unscoped role at system scope.
"""

import pytest

from dfe_engine.governance.ch.models import org_user_name
from dfe_engine.secrets import build_secrets
from tests.support.held_callers import caller_in_group, define_role


@pytest.fixture
def ch_secrets(api_settings):
    store = build_secrets(api_settings.secrets)
    store.put("ch/orgs/acme", "acme-pw")
    store.put("ch/service/query_reader", "platform-pw")
    return store


def test_a_scoped_role_gets_its_orgs_connection(app, client, api_settings, ch_secrets):
    app.state.org_registry.create("acme", org_ids=["t-acme"])
    define_role(app, "tenant_search", ["query:execute"], scoped=True)
    tenant = caller_in_group(app, api_settings, "tenant", roles=["tenant_search"], org_ids=["acme"])

    conn = tenant.get("/api/v1/hyperdx/connection")
    me = tenant.get("/api/v1/auth/me")

    assert conn.status_code == 200, conn.text
    assert conn.json()["username"] == org_user_name("acme")
    assert conn.json()["password"] == "acme-pw"
    assert me.json()["hyperdx_identity"] == org_user_name("acme")


def test_the_same_role_unscoped_gets_the_platform_reader(app, client, api_settings, ch_secrets):
    app.state.org_registry.create("acme", org_ids=["t-acme"])
    define_role(app, "platform_search", ["query:execute"], scoped=False)
    platform = caller_in_group(
        app, api_settings, "platform", roles=["platform_search"], org_ids=["acme"]
    )

    conn = platform.get("/api/v1/hyperdx/connection")

    assert conn.status_code == 200, conn.text
    assert conn.json()["username"] == "dfe_query_reader"


def test_an_org_only_another_group_names_does_not_widen_the_connection(
    app, client, api_settings, ch_secrets
):
    """org_viewer system-wide over acme, plus infra_viewer (no query:execute) bound at beta."""
    app.state.org_registry.create("acme", org_ids=["t-acme"])
    app.state.org_registry.create("beta")
    app.state.group_store.create("beta-infra", roles=["infra_viewer"], scope="org:beta")
    app.state.group_store.update("beta-infra", org_ids=["beta"])
    viewer = caller_in_group(app, api_settings, "mixed", roles=["org_viewer"], org_ids=["acme"])
    app.state.group_store.add_member("beta-infra", "mixed")
    app.state.account_store.update("mixed", groups=["mixed-group", "beta-infra"])

    conn = viewer.get("/api/v1/hyperdx/connection")

    assert conn.status_code == 200, conn.text
    assert conn.json()["username"] == org_user_name("acme")
