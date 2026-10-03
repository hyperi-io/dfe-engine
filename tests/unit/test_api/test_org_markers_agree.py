#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_org_markers_agree.py
#  Purpose:      HyperDX, the CH group bindings and the sampler resolve an org marker alike
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A group's org marker means the same org on every path that reads its rows.

A group may list an org by its name or by one of its tenant ids. Either way the
HyperDX connection read hands its members that org's pinned user, the group's
ClickHouse user is pinned to that org's tenant ids, and a held sample binds those
same ids. A marker no org declares gets nothing on all three, and is refused
where a read would otherwise follow.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.api.deps import get_clickhouse_client
from dfe_engine.governance.ch.bootstrap import reconcile_from_stores
from dfe_engine.governance.ch.models import TENANT_SETTING, group_user_name, org_user_name
from tests.support.held_callers import caller_in_group

EVENTS = "`db`.`events`"
ACME_TENANTS = ["t-acme-1", "t-acme-2"]


class _Result:
    def __init__(self, rows: list[list[str]]) -> None:
        self.result_rows = rows


class _Table:
    """A table carrying ``_org_id``: DESCRIBE answers, and every row read is recorded."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, settings=None):
        if sql.startswith("DESCRIBE TABLE"):
            return _Result([["_org_id", "String"], ["_json", "String"]])
        self.reads.append((sql, parameters or {}))
        return _Result([['{"a": 1}']])


class _AdminClient:
    """Records the DDL a reconcile executes; every discovery query returns no rows."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def query(self, sql: str, parameters: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(result_rows=[])

    def command(self, stmt: str) -> None:
        self.executed.append(stmt)


@pytest.fixture
def table(app):
    ch = _Table()
    app.dependency_overrides[get_clickhouse_client] = lambda: ch
    yield ch
    app.dependency_overrides.pop(get_clickhouse_client, None)


@pytest.fixture
def acme(app, client):
    """The org ``acme``, whose rows carry two tenant ids that are not its name."""
    app.state.org_registry.create("acme", org_ids=ACME_TENANTS)


def _member(app, api_settings, name: str, marker: str):
    return caller_in_group(app, api_settings, name, roles=["org_viewer"], org_ids=[marker])


def _identity(caller) -> str:
    me = caller.get("/api/v1/auth/me")
    assert me.status_code == 200, me.text
    return me.json()["hyperdx_identity"]


def _pins(app, api_settings) -> dict[str, str]:
    """Each group user's pinned tenant ids, from a reconcile over the app's own stores."""
    client = _AdminClient()
    reconcile_from_stores(
        client,
        settings=api_settings,
        org_registry=app.state.org_registry,
        group_store=app.state.group_store,
        role_config=app.state.role_config,
    )
    pins: dict[str, str] = {}
    for stmt in client.executed:
        if TENANT_SETTING not in stmt or not stmt.startswith("ALTER USER"):
            continue
        user = stmt.split(" ")[2].strip("`")
        pins[user] = stmt.rsplit(" = ", 1)[1].removesuffix(" READONLY").strip("'")
    return pins


def _sample(caller):
    return caller.post("/api/v1/sample", json={"mode": "recent", "table": EVENTS})


def test_hyperdx_hands_a_name_and_a_tenant_id_the_same_org_user(app, api_settings, acme):
    by_name = _member(app, api_settings, "by-name", "acme")
    by_tenant = _member(app, api_settings, "by-tenant", "t-acme-2")

    assert _identity(by_name) == _identity(by_tenant) == org_user_name("acme")


def test_the_ch_bindings_pin_a_name_and_a_tenant_id_to_the_same_tenant_ids(app, api_settings, acme):
    _member(app, api_settings, "by-name", "acme")
    _member(app, api_settings, "by-tenant", "t-acme-2")

    pins = _pins(app, api_settings)

    expected = ",".join(ACME_TENANTS)
    assert pins[group_user_name("by-name-group")] == expected
    assert pins[group_user_name("by-tenant-group")] == expected


def test_the_sampler_binds_a_name_and_a_tenant_id_to_the_same_tenant_ids(
    app, api_settings, acme, table
):
    by_name = _member(app, api_settings, "by-name", "acme")
    by_tenant = _member(app, api_settings, "by-tenant", "t-acme-2")

    assert _sample(by_name).status_code == 200
    assert _sample(by_tenant).status_code == 200

    [(_, named), (_, tenant)] = table.reads
    assert named["orgs"] == tenant["orgs"] == ACME_TENANTS


def test_an_unknown_marker_gets_nothing_on_every_path(app, api_settings, acme, table):
    stray = _member(app, api_settings, "stray", "t-nobody")

    connection = stray.get("/api/v1/hyperdx/connection")
    sample = _sample(stray)

    assert _identity(stray) == ""
    assert connection.status_code == 403, connection.text
    assert connection.json()["code"] == "no_single_org"
    assert group_user_name("stray-group") not in _pins(app, api_settings)
    assert sample.status_code == 403, sample.text
    assert "no registered org" in sample.json()["message"]
    assert table.reads == []


def test_a_marker_naming_one_org_is_that_org_on_every_path(app, client, api_settings, acme, table):
    # A second org is NAMED by acme's first tenant id; the name decides, on every path.
    app.state.org_registry.create("t-acme-1", org_ids=["t-other"])
    member = _member(app, api_settings, "named", "t-acme-1")

    assert _identity(member) == org_user_name("t-acme-1")
    assert _pins(app, api_settings)[group_user_name("named-group")] == "t-other"
    assert _sample(member).status_code == 200
    [(_, params)] = table.reads
    assert params["orgs"] == ["t-other"]
