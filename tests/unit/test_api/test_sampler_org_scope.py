#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sampler_org_scope.py
#  Purpose:      A sampler caller without a platform grant reads only its own orgs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The sampler holds a caller without a platform grant to its own orgs.

``dfe-multi-viewer`` is an org_viewer whose group is bound at SYSTEM scope with two
orgs, so it passes the system-scope ``sampler:read`` check. Every read it makes
must bind ``_org_id`` to those two orgs, a target that cannot be held to them is
refused, and it never sees a sample another caller took across every org. The
real-ClickHouse proof that the binding returns only those rows is
``tests/integration/test_sampler_org_scope.py``.
"""

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token, get_clickhouse_client
from dfe_engine.sampling.clickhouse_reader import build_where

MULTI_ORGS = ["test_org", "test_org_2"]
ROUTES = ["/api/v1/sample", "/api/v1/sources/filebeat/sample"]
EVENTS = "`db`.`events`"


class _Result:
    def __init__(self, rows: list[list[str]]) -> None:
        self.result_rows = rows


class OrgTable:
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
def table(app, client):
    """The sampled table, with the fixture orgs registered under tenant ids equal to their names."""
    for org in MULTI_ORGS:
        app.state.org_registry.create(org, org_ids=[org])
    ch = OrgTable(["_org_id", "_json", "timestamp_load"])
    app.dependency_overrides[get_clickhouse_client] = lambda: ch
    yield ch
    app.dependency_overrides.pop(get_clickhouse_client, None)


@pytest.fixture
def viewer_of(client, app, api_settings):
    """Return a factory: ``viewer_of(name, orgs)`` -> an org_viewer bound at system scope."""

    def _make(name: str, org_ids: list[str]) -> TestClient:
        group = f"{name}-group"
        app.state.group_store.create(group, roles=["org_viewer"])
        app.state.group_store.update(group, org_ids=org_ids)
        app.state.account_store.create(name, f"{name}-password", groups=[group])
        app.state.group_store.add_member(group, name)
        token = create_access_token(data={"sub": name}, settings=api_settings)
        caller = TestClient(app, raise_server_exceptions=False)
        caller.headers.update({"Authorization": f"Bearer {token}"})
        return caller

    return _make


def _sample(caller: TestClient, route: str = "/api/v1/sample", **body):
    return caller.post(route, json={"mode": "recent", "table": EVENTS, **body})


@pytest.mark.parametrize("route", ROUTES)
def test_a_caller_without_a_platform_grant_reads_only_its_own_orgs(client_as, table, route):
    resp = _sample(client_as("dfe-multi-viewer"), route)

    assert resp.status_code == 200, resp.text
    [(sql, params)] = table.reads
    assert "WHERE _org_id IN {orgs:Array(String)}" in sql
    assert params["orgs"] == MULTI_ORGS


def test_its_filter_cannot_widen_the_orgs(client_as, table):
    resp = _sample(client_as("dfe-multi-viewer"), filter="1 = 1 OR _org_id != ''")

    assert resp.status_code == 200, resp.text
    [(sql, params)] = table.reads
    assert "WHERE _org_id IN {orgs:Array(String)} AND (1 = 1 OR _org_id <> '')" in sql
    assert params["orgs"] == MULTI_ORGS


@pytest.mark.parametrize("identity", ["dfe-admin", "dfe-viewer", "dfe-analyst", "dfe-infra-admin"])
def test_a_platform_reader_reads_every_org(client_as, table, identity):
    resp = _sample(client_as(identity))

    assert resp.status_code == 200, resp.text
    [(sql, params)] = table.reads
    assert "_org_id" not in sql
    assert "orgs" not in params


def test_a_caller_in_no_org_is_refused_before_any_read(viewer_of, table):
    resp = _sample(viewer_of("orgless-viewer", []))

    assert resp.status_code == 403, resp.text
    assert "no registered org" in resp.json()["message"]
    assert table.reads == []


def test_a_table_without_org_id_is_refused_to_a_caller_held_to_its_orgs(client_as, table):
    table.columns = ["_json", "timestamp_load"]

    resp = _sample(client_as("dfe-multi-viewer"))

    assert resp.status_code == 403, resp.text
    assert f"{EVENTS} has no _org_id column" in resp.json()["message"]
    assert table.reads == []


def test_a_platform_reader_still_samples_a_table_without_org_id(client_as, table):
    table.columns = ["_json", "timestamp_load"]

    resp = _sample(client_as("dfe-admin"))

    assert resp.status_code == 200, resp.text
    assert len(table.reads) == 1


def test_a_kafka_topic_is_refused_to_a_caller_held_to_its_orgs(client_as, table):
    resp = client_as("dfe-multi-viewer").post(
        "/api/v1/sample", json={"mode": "recent", "backend": "kafka", "topic": "events"}
    )

    assert resp.status_code == 403, resp.text
    assert "Kafka topic has no _org_id column" in resp.json()["message"]


def test_a_sample_across_every_org_is_hidden_from_a_caller_held_to_its_orgs(client_as, table):
    admin, viewer = client_as("dfe-admin"), client_as("dfe-multi-viewer")
    every_org = _sample(admin).json()["task_id"]
    own = _sample(viewer).json()["task_id"]

    assert viewer.get(f"/api/v1/samples/{every_org}").status_code == 404
    assert [task["id"] for task in viewer.get("/api/v1/samples").json()] == [own]
    assert viewer.get(f"/api/v1/samples/{own}").json()["result"]["count"] == 1
    assert {task["id"] for task in admin.get("/api/v1/samples").json()} == {every_org, own}


def test_a_caller_in_fewer_orgs_does_not_see_a_sample_held_to_more(client_as, viewer_of, table):
    narrow = viewer_of("one-org-viewer", ["test_org"])
    wide = _sample(client_as("dfe-multi-viewer")).json()["task_id"]
    own = _sample(narrow).json()["task_id"]

    assert narrow.get(f"/api/v1/samples/{wide}").status_code == 404
    assert [task["id"] for task in narrow.get("/api/v1/samples").json()] == [own]
    [_, (_, params)] = table.reads
    assert params["orgs"] == ["test_org"]


def test_the_where_builder_binds_the_orgs_and_refuses_none():
    where, params = build_where(
        source_label=None,
        org_ids=MULTI_ORGS,
        filter_sql=None,
        since=None,
        until=None,
        timestamp_field="timestamp_load",
    )
    assert where == "WHERE _org_id IN {orgs:Array(String)}"
    assert params == {"orgs": MULTI_ORGS}

    with pytest.raises(ValueError, match="no org"):
        build_where(
            source_label=None,
            org_ids=[],
            filter_sql=None,
            since=None,
            until=None,
            timestamp_field="timestamp_load",
        )
