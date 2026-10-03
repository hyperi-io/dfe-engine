#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_schema_rows_org_scope.py
#  Purpose:      The schema routes that read raw rows hold a held caller to its orgs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``sample-rows``, ``json-paths`` and ``promote-field`` bind ``_org_id`` as the sampler does.

These read the same landing and source tables the sampler reads. A caller held to
its orgs (here a ``scoped`` role holding ``schema:read`` and ``schema:write``) has
every row-reading query bound to its orgs' tenant ids, and a table it cannot be
held on is refused before any row is read. A platform caller reads as before.
"""

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token, get_clickhouse_client
from dfe_engine.settings import DFESettings, get_settings
from tests.support.accounts import admin_on_its_own_password
from tests.support.held_callers import caller_in_group, define_role
from tests.unit.test_api.test_json_promotion import (
    NOMETA_SOURCE,
    PROMO_SOURCE,
    make_api_settings,
)

HELD = "_org_id IN {orgs:Array(String)}"
MATCH = "toString(assumeNotNull(_json).`tags.collector.type`) = {match_value:String}"


class _Rows:
    """A ClickHouse stand-in for the landing table; records every query it is sent."""

    def __init__(self, *, org_id_column: bool = True) -> None:
        self.org_id_column = org_id_column
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql: str, parameters: dict | None = None):
        self.calls.append((sql, parameters or {}))
        if "system.columns" in sql:
            return [(1,)] if self.org_id_column else []
        if "JSONDynamicPathsWithTypes" in sql:
            return [("user.id", "Int64")]
        if "SELECT DISTINCT" in sql:
            return [("1",)]
        if "coverage_pct" in sql:
            return [(100.0, 1)]
        return []

    def query_rows(self, sql: str, parameters: dict | None = None):
        self.calls.append((sql, parameters or {}))
        return ["_json", "_org_id"], [({"user": {"id": 1}}, "t-acme")]

    def row_reads(self) -> list[tuple[str, dict]]:
        """Every query that reads the table's rows, not ClickHouse's own catalogue."""
        return [(sql, params) for sql, params in self.calls if "system." not in sql]


@pytest.fixture
def settings(tmp_path) -> DFESettings:
    return make_api_settings(tmp_path)


@pytest.fixture
def ch() -> _Rows:
    return _Rows()


@pytest.fixture
def app(settings, ch):
    application = create_app(settings)
    application.dependency_overrides[get_clickhouse_client] = lambda: ch
    try:
        yield application
    finally:
        _registries.clear()


@pytest.fixture
def client(app) -> TestClient:
    with TestClient(app, raise_server_exceptions=False) as c:
        admin_on_its_own_password(app)
        yield c


@pytest.fixture
def admin_headers(settings) -> dict[str, str]:
    token = create_access_token(data={"sub": "admin"}, settings=settings)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def tenant(app, client, settings) -> TestClient:
    """A caller whose scoped role holds schema:read and schema:write, in the org acme."""
    app.state.org_registry.create("acme", org_ids=["t-acme"])
    define_role(app, "tenant_schema", ["schema:read", "schema:write"], scoped=True)
    return caller_in_group(app, settings, "tenant", roles=["tenant_schema"], org_ids=["acme"])


def _landing() -> str:
    clickhouse = get_settings().clickhouse
    return f"`{clickhouse.effective_data_database}`.`{clickhouse.landing_table}`"


def test_sample_rows_binds_the_held_callers_tenant_ids(tenant, ch):
    resp = tenant.get(f"/api/v1/schemas/{NOMETA_SOURCE}/sample-rows")

    assert resp.status_code == 200, resp.text
    [(sql, params)] = ch.row_reads()
    assert f"FROM {_landing()} WHERE {HELD} AND ({MATCH})" in sql
    assert params["orgs"] == ["t-acme"]
    assert params["match_value"] == NOMETA_SOURCE


def test_every_json_paths_query_binds_the_held_callers_tenant_ids(tenant, ch):
    resp = tenant.get(
        f"/api/v1/schemas/{NOMETA_SOURCE}/json-paths", params={"samples": 2, "stats": True}
    )

    assert resp.status_code == 200, resp.text
    reads = ch.row_reads()
    assert len(reads) == 3
    for sql, params in reads:
        assert f"{HELD} AND ({MATCH})" in sql
        assert params["orgs"] == ["t-acme"]


def test_promote_field_discovery_binds_the_held_callers_tenant_ids(tenant, ch):
    resp = tenant.post(
        f"/api/v1/schemas/{PROMO_SOURCE}/promote-field",
        json={"json_path": "user.id"},
        params={"dry_run": True},
    )

    assert resp.status_code == 200, resp.text
    [(sql, params)] = ch.row_reads()
    assert "JSONDynamicPathsWithTypes" in sql
    assert HELD in sql
    assert params["orgs"] == ["t-acme"]


@pytest.mark.parametrize("route", ["sample-rows", "json-paths"])
def test_a_table_without_org_id_is_refused_before_any_row_is_read(tenant, ch, route):
    ch.org_id_column = False

    resp = tenant.get(f"/api/v1/schemas/{NOMETA_SOURCE}/{route}")

    assert resp.status_code == 403, resp.text
    assert "has no _org_id column" in resp.json()["message"]
    assert ch.row_reads() == []


def test_a_held_caller_in_no_registered_org_is_refused(app, client, settings, ch):
    define_role(app, "stray_schema", ["schema:read"], scoped=True)
    stray = caller_in_group(app, settings, "stray", roles=["stray_schema"], org_ids=["nope"])

    resp = stray.get(f"/api/v1/schemas/{NOMETA_SOURCE}/sample-rows")

    assert resp.status_code == 403, resp.text
    assert "no registered org" in resp.json()["message"]
    assert ch.calls == []


@pytest.mark.parametrize("route", ["sample-rows", "json-paths"])
def test_a_platform_caller_reads_every_org_with_no_column_check(client, admin_headers, ch, route):
    resp = client.get(f"/api/v1/schemas/{NOMETA_SOURCE}/{route}", headers=admin_headers)

    assert resp.status_code == 200, resp.text
    assert all("_org_id" not in sql for sql, _ in ch.calls)
    assert all("orgs" not in params for _, params in ch.calls)
