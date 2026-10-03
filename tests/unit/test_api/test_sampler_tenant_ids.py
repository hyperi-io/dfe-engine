#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sampler_tenant_ids.py
#  Purpose:      A held sample binds the tenant ids the row policy binds, and scoped roles are held
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a held caller's sample binds, and which callers are held.

A group lists org markers; the ClickHouse row policy pins each org's user to that
org's tenant ids. The sampler binds the same tenant ids, so a sample and a
HyperDX query of one org read the same rows, and a marker no org declares binds
nothing. A role marked ``scoped`` is held wherever it is bound, including one an
admin creates through the roles API.
"""

import pytest

from dfe_engine.api.deps import get_clickhouse_client
from dfe_engine.sampling import Sampler, SampleRequest
from tests.support.held_callers import caller_in_group, define_role
from tests.unit.test_api.test_apps import BASE, _deploy, _wire

EVENTS = "`db`.`events`"
ACME_TENANTS = ["t-acme-1", "t-acme-2"]


class _Result:
    def __init__(self, rows: list[list[str]]) -> None:
        self.result_rows = rows


class _Table:
    """One ClickHouse table: DESCRIBE lists ``columns``, and every read is recorded."""

    def __init__(self, columns: list[str]) -> None:
        self.columns = columns
        self.reads: list[tuple[str, dict]] = []

    def query(self, sql, parameters=None, settings=None):
        if sql.startswith("DESCRIBE TABLE"):
            return _Result([[name, "String"] for name in self.columns])
        self.reads.append((sql, parameters or {}))
        return _Result([['{"a": 1}']])


@pytest.fixture
def table(app):
    ch = _Table(["_org_id", "_json", "timestamp_load"])
    app.dependency_overrides[get_clickhouse_client] = lambda: ch
    yield ch
    app.dependency_overrides.pop(get_clickhouse_client, None)


@pytest.fixture
def acme(app, client):
    """The org ``acme``, whose rows carry two tenant ids that are not its name."""
    app.state.org_registry.create("acme", org_ids=ACME_TENANTS)


def _sample(caller, **body):
    return caller.post("/api/v1/sample", json={"mode": "recent", "table": EVENTS, **body})


def test_the_sample_binds_the_orgs_tenant_ids_not_its_name(app, api_settings, table, acme):
    viewer = caller_in_group(
        app, api_settings, "acme-viewer", roles=["org_viewer"], org_ids=["acme"]
    )

    resp = _sample(viewer)

    assert resp.status_code == 200, resp.text
    [(sql, params)] = table.reads
    assert "WHERE _org_id IN {orgs:Array(String)}" in sql
    assert params["orgs"] == ACME_TENANTS


def test_an_org_declaring_no_tenant_ids_binds_its_name(app, client, api_settings, table):
    app.state.org_registry.create("beta", org_ids=[])
    viewer = caller_in_group(
        app, api_settings, "beta-viewer", roles=["org_viewer"], org_ids=["beta"]
    )

    resp = _sample(viewer)

    assert resp.status_code == 200, resp.text
    [(_, params)] = table.reads
    assert params["orgs"] == ["beta"]


def test_a_group_naming_no_registered_org_is_refused_before_any_read(
    app, api_settings, table, acme
):
    viewer = caller_in_group(
        app, api_settings, "stray-viewer", roles=["org_viewer"], org_ids=["t-nobody"]
    )

    resp = _sample(viewer)

    assert resp.status_code == 403, resp.text
    assert "no registered org" in resp.json()["message"]
    assert table.reads == []


def test_a_scoped_custom_role_at_system_scope_is_held_to_its_orgs(app, api_settings, table, acme):
    define_role(app, "tenant_viewer", ["sampler:read"], scoped=True)
    tenant = caller_in_group(app, api_settings, "tenant", roles=["tenant_viewer"], org_ids=["acme"])

    resp = _sample(tenant)

    assert resp.status_code == 200, resp.text
    [(sql, params)] = table.reads
    assert "WHERE _org_id IN {orgs:Array(String)}" in sql
    assert params["orgs"] == ACME_TENANTS


def test_the_same_role_unscoped_reads_every_org(app, api_settings, table, acme):
    define_role(app, "platform_viewer", ["sampler:read"], scoped=False)
    platform = caller_in_group(
        app, api_settings, "platform", roles=["platform_viewer"], org_ids=["acme"]
    )

    resp = _sample(platform)

    assert resp.status_code == 200, resp.text
    [(sql, params)] = table.reads
    assert "_org_id" not in sql
    assert "orgs" not in params


def test_a_role_created_scoped_through_the_roles_api_is_held(
    app, client, api_settings, admin_headers, table, acme
):
    created = client.post(
        "/api/v1/auth/roles",
        json={"name": "api_tenant", "permissions": ["sampler:read"], "scoped": True},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    tenant = caller_in_group(
        app, api_settings, "api-tenant", roles=["api_tenant"], org_ids=["acme"]
    )

    resp = _sample(tenant)

    assert resp.status_code == 200, resp.text
    [(_, params)] = table.reads
    assert params["orgs"] == ACME_TENANTS


@pytest.mark.parametrize("mode", ["smart", "anomaly"])
def test_the_gated_modes_scan_only_the_held_orgs(api_settings, mode):
    sampler = Sampler(api_settings.sampler, api_settings.kafka, api_settings.clickhouse)
    req = SampleRequest(mode=mode, table=EVENTS, filter="1 = 1 OR _org_id != ''")

    sql, params = sampler.reduce_query(req, EVENTS, None, ACME_TENANTS)

    assert sql.startswith(f"SELECT toString(_json) FROM {EVENTS} ")
    assert "WHERE _org_id IN {orgs:Array(String)} AND (1 = 1 OR _org_id <> '')" in sql
    assert params["orgs"] == ACME_TENANTS


@pytest.mark.parametrize("mode", ["smart", "anomaly"])
def test_the_gated_modes_bind_no_org_for_a_platform_reader(api_settings, mode):
    sampler = Sampler(api_settings.sampler, api_settings.kafka, api_settings.clickhouse)

    sql, params = sampler.reduce_query(SampleRequest(mode=mode, table=EVENTS), EVENTS, None, None)

    assert "_org_id" not in sql
    assert "orgs" not in params


@pytest.mark.parametrize("mode", ["smart", "anomaly"])
def test_a_gated_mode_held_to_no_org_reads_nothing(api_settings, mode):
    sampler = Sampler(api_settings.sampler, api_settings.kafka, api_settings.clickhouse)

    with pytest.raises(ValueError, match="no org"):
        sampler.reduce_query(SampleRequest(mode=mode, table=EVENTS), EVENTS, None, [])


@pytest.mark.parametrize("mode", ["smart", "anomaly"])
def test_a_gated_sample_of_a_table_without_org_id_is_refused_to_a_held_caller(
    app, api_settings, table, acme, mode
):
    table.columns = ["_json", "timestamp_load"]
    viewer = caller_in_group(
        app, api_settings, f"{mode}-viewer", roles=["org_viewer"], org_ids=["acme"]
    )

    resp = _sample(viewer, mode=mode)

    assert resp.status_code == 403, resp.text
    assert "has no _org_id column" in resp.json()["message"]
    assert table.reads == []


@pytest.fixture
def dry_runner(app, client, api_settings, admin_headers, acme, tmp_path):
    """A caller holding dryrun:execute, held to acme for samples, on a deployed instance."""
    _wire(app, tmp_path)
    _deploy(client, admin_headers)
    define_role(app, "dry-runner", ["dryrun:execute"], scoped=False)
    return caller_in_group(
        app, api_settings, "runner", roles=["dry-runner", "org_viewer"], org_ids=["acme"]
    )


def _dry_run(caller):
    return caller.post(
        f"{BASE}/files/transforms/dry-run",
        json={"name": "000.vrl", "content": ". = parse_json!(.message)\n"},
    )


def test_a_dry_run_through_the_real_sampler_refuses_a_table_it_cannot_hold(dry_runner, table):
    table.columns = ["_json", "timestamp_load"]

    resp = _dry_run(dry_runner)

    assert resp.status_code == 403, resp.text
    assert "has no _org_id column" in resp.json()["message"]
    assert table.reads == []


def test_a_dry_run_through_the_real_sampler_refuses_a_caller_in_no_registered_org(
    app, api_settings, dry_runner, table
):
    stray = caller_in_group(
        app, api_settings, "stray-runner", roles=["dry-runner", "org_viewer"], org_ids=["nope"]
    )

    resp = _dry_run(stray)

    assert resp.status_code == 403, resp.text
    assert "no registered org" in resp.json()["message"]
    assert table.reads == []
