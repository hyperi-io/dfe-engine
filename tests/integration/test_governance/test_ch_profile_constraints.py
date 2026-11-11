#  Project:      dfe-engine
#  File:         tests/integration/test_governance/test_ch_profile_constraints.py
#  Purpose:      A reader's caps hold against its own queries on a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The shipped reader profiles refuse a query that lifts a cap, and accept HyperDX's settings.

readonly=2 lets a query change any setting its profile does not constrain. The
settings go as HTTP query parameters, as HyperDX sends them.
"""

import uuid

import httpx
import pytest

from dfe_engine.governance.ch.bootstrap import service_roles_for
from dfe_engine.governance.ch.models import DEFAULT_TIERS
from dfe_engine.governance.ch.reconciler import ChRbacReconciler, resolve_grant_databases
from dfe_engine.governance.ch.render import render_tier
from dfe_engine.settings import DFESettings

pytestmark = pytest.mark.integration

SETTING_CONSTRAINT_VIOLATION = "452"

# What HyperDX's ClickHouse client adds to every query, its team queryTimeout included.
HYPERDX_DEFAULTS = {
    "allow_experimental_analyzer": "1",
    "date_time_output_format": "iso",
    "wait_end_of_query": "0",
    "cancel_http_readonly_queries_on_client_close": "1",
    "output_format_json_quote_64bit_integers": "1",
    "max_execution_time": "60",
}

ACCEPTED = [
    pytest.param({}, id="hyperdx-defaults"),
    pytest.param(
        {"max_execution_time": "15", "timeout_overflow_mode": "break", "max_rows_to_read": "0"},
        id="hyperdx-key-values",
    ),
    pytest.param({"readonly": "2"}, id="hyperdx-alerts-readonly"),
    pytest.param({"max_execution_time": "5"}, id="lower-time"),
    pytest.param({"max_memory_usage": "1048576"}, id="lower-memory"),
]

REFUSED = [
    pytest.param({"max_execution_time": "0"}, id="time-to-unlimited"),
    pytest.param({"max_execution_time": "61"}, id="time-above-the-ceiling"),
    pytest.param({"max_memory_usage": "0"}, id="memory-to-unlimited"),
]


def _renamed(entry, name: str):
    return entry.model_copy(update={"name": name})


@pytest.fixture
def reader(ch_client, ch_params, clickhouse_test_database):
    """The shipped query_reader profile, reconciled under a uid name, with a user holding it."""
    name = f"probe{uuid.uuid4().hex[:8]}"
    (shipped,) = [r for r in service_roles_for(DFESettings(env="dev")) if r.name == "query_reader"]
    role = _renamed(shipped, name).model_copy(update={"grants": ["SELECT ON {db}.*"]})
    result = ChRbacReconciler(ch_client, database=clickhouse_test_database).reconcile_service_roles(
        [role]
    )
    assert result.errors == []
    # The minted user's own grant and profile, as render_service_user writes them.
    password = uuid.uuid4().hex
    user = f"`{role.user()}`"
    ch_client.command(f"CREATE USER {user} IDENTIFIED WITH plaintext_password BY '{password}'")
    ch_client.command(f"GRANT `{role.role()}` TO {user}")
    ch_client.command(f"ALTER USER {user} SETTINGS PROFILE `{role.profile()}`")
    try:
        yield role, ch_params, password
    finally:
        ch_client.command(f"DROP USER IF EXISTS `{role.user()}`")
        ch_client.command(f"DROP ROLE IF EXISTS `{role.role()}`")
        ch_client.command(f"DROP SETTINGS PROFILE IF EXISTS `{role.profile()}`")


@pytest.fixture
def tier_user(ch_client, ch_params, clickhouse_test_database):
    """analyst_tier_3, the tightest time cap, held through its role as an org user holds it."""
    name = f"probe{uuid.uuid4().hex[:8]}"
    (shipped,) = [t for t in DEFAULT_TIERS if t.name == "analyst_tier_3"]
    (tier,) = resolve_grant_databases(
        [_renamed(shipped, name).model_copy(update={"grants": ["SELECT ON {db}.*"]})],
        clickhouse_test_database,
    )
    for statement in render_tier(tier):
        ch_client.command(statement)
    user, password = f"{name}_u", uuid.uuid4().hex
    ch_client.command(f"CREATE USER `{user}` IDENTIFIED WITH plaintext_password BY '{password}'")
    ch_client.command(f"GRANT `{tier.role()}` TO `{user}`")
    try:
        yield user, ch_params, password
    finally:
        ch_client.command(f"DROP USER IF EXISTS `{user}`")
        ch_client.command(f"DROP QUOTA IF EXISTS `{tier.quota_name()}`")
        ch_client.command(f"DROP ROLE IF EXISTS `{tier.role()}`")
        ch_client.command(f"DROP SETTINGS PROFILE IF EXISTS `{tier.profile()}`")


def _query(params: dict, user: str, password: str, settings: dict[str, str]) -> httpx.Response:
    """``SELECT 1`` as ``user`` over HTTP with HyperDX's settings plus ``settings``."""
    scheme = "https" if params.get("secure") else "http"
    return httpx.get(
        f"{scheme}://{params['host']}:{params['port']}/",
        params={**HYPERDX_DEFAULTS, **settings, "query": "SELECT 1"},
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
        timeout=30,
    )


def test_a_profile_from_before_the_constraints_is_altered_to_them(
    ch_client, clickhouse_test_database
):
    """An upgrade meets the profile an older release created with plain values."""
    name = f"probe{uuid.uuid4().hex[:8]}"
    (shipped,) = [r for r in service_roles_for(DFESettings(env="dev")) if r.name == "query_reader"]
    role = _renamed(shipped, name).model_copy(update={"grants": ["SELECT ON {db}.*"]})
    ch_client.command(
        f"CREATE SETTINGS PROFILE `{role.profile()}` SETTINGS readonly = 2, "
        "max_execution_time = 30, max_memory_usage = 2147483648"
    )
    try:
        result = ChRbacReconciler(
            ch_client, database=clickhouse_test_database
        ).reconcile_service_roles([role])
        rows = ch_client.query(
            "SELECT setting_name, value, min, max FROM system.settings_profile_elements "
            "WHERE profile_name = {p:String} AND setting_name IN "
            "('max_execution_time', 'max_memory_usage') ORDER BY setting_name",
            parameters={"p": role.profile()},
        ).result_rows
    finally:
        ch_client.command(f"DROP ROLE IF EXISTS `{role.role()}`")
        ch_client.command(f"DROP SETTINGS PROFILE IF EXISTS `{role.profile()}`")

    assert result.errors == []
    assert [tuple(map(str, row)) for row in rows] == [
        ("max_execution_time", "30", "1", "60"),
        ("max_memory_usage", "2147483648", "1", "2147483648"),
    ]


@pytest.mark.parametrize("settings", ACCEPTED)
def test_the_reader_accepts_what_hyperdx_sends(reader, settings):
    role, params, password = reader
    response = _query(params, role.user(), password, settings)
    assert response.status_code == 200, response.text
    assert response.text.strip() == "1"


@pytest.mark.parametrize("settings", REFUSED)
def test_the_reader_refuses_a_lifted_cap(reader, settings):
    role, params, password = reader
    response = _query(params, role.user(), password, settings)
    assert response.headers.get("X-ClickHouse-Exception-Code") == SETTING_CONSTRAINT_VIOLATION


@pytest.mark.parametrize("settings", ACCEPTED)
def test_a_tier_held_through_its_role_accepts_what_hyperdx_sends(tier_user, settings):
    user, params, password = tier_user
    response = _query(params, user, password, settings)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("settings", REFUSED)
def test_a_tier_held_through_its_role_refuses_a_lifted_cap(tier_user, settings):
    user, params, password = tier_user
    response = _query(params, user, password, settings)
    assert response.headers.get("X-ClickHouse-Exception-Code") == SETTING_CONSTRAINT_VIOLATION
