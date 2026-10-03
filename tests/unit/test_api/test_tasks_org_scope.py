#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_tasks_org_scope.py
#  Purpose:      The generic task routes honour a task's org hold, as GET /samples does
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``/tasks`` shows a held caller only the tasks held to its orgs.

A sample task's result is the sampled rows, so the generic list, get, stream and
cancel routes must not hand a tenant another org's sample, or one taken across
every org. A caller whose ``task:read`` comes from a platform role still sees
every task.
"""

import pytest

from dfe_engine.api.deps import get_clickhouse_client
from tests.support.held_callers import caller_in_group, define_role

EVENTS = "`db`.`events`"


class _Result:
    def __init__(self, rows: list[list[str]]) -> None:
        self.result_rows = rows


class _Table:
    """A table carrying ``_org_id``: DESCRIBE answers, and every read returns one row."""

    def query(self, sql, parameters=None, settings=None):
        if sql.startswith("DESCRIBE TABLE"):
            return _Result([["_org_id", "String"], ["_json", "String"]])
        return _Result([['{"a": 1}']])


@pytest.fixture
def tasks(app, client, api_settings, admin_headers):
    """An admin's sample across every org, and a tenant's sample held to acme."""
    app.dependency_overrides[get_clickhouse_client] = _Table
    app.state.org_registry.create("acme", org_ids=["t-acme"])
    define_role(app, "tenant_tasks", ["task:read", "task:write", "sampler:read"], scoped=True)
    tenant = caller_in_group(app, api_settings, "tenant", roles=["tenant_tasks"], org_ids=["acme"])
    body = {"mode": "recent", "table": EVENTS}
    every_org = client.post("/api/v1/sample", json=body, headers=admin_headers).json()["task_id"]
    own = tenant.post("/api/v1/sample", json=body).json()["task_id"]
    yield tenant, every_org, own
    app.dependency_overrides.pop(get_clickhouse_client, None)


def test_the_list_shows_a_held_caller_only_its_own_orgs_tasks(tasks):
    tenant, _every_org, own = tasks

    resp = tenant.get("/api/v1/tasks")

    assert resp.status_code == 200, resp.text
    assert [task["id"] for task in resp.json()] == [own]


def test_a_task_across_every_org_is_not_found_for_a_held_caller(tasks):
    tenant, every_org, own = tasks

    assert tenant.get(f"/api/v1/tasks/{every_org}").status_code == 404
    assert tenant.get(f"/api/v1/tasks/{own}").json()["result"]["count"] == 1


def test_a_held_caller_cannot_stream_or_cancel_another_orgs_task(tasks):
    tenant, every_org, _own = tasks

    assert tenant.get(f"/api/v1/tasks/{every_org}/stream").status_code == 404
    assert tenant.post(f"/api/v1/tasks/{every_org}/cancel").status_code == 404


def test_a_task_held_to_another_org_is_hidden(app, client, api_settings, tasks):
    _tenant, _every_org, own = tasks
    app.state.org_registry.create("beta", org_ids=["t-beta"])
    other = caller_in_group(
        app, api_settings, "beta-tenant", roles=["tenant_tasks"], org_ids=["beta"]
    )

    assert other.get(f"/api/v1/tasks/{own}").status_code == 404
    assert other.get("/api/v1/tasks").json() == []


def test_a_held_caller_sees_no_task_that_carries_no_hold(client, admin_headers, tasks):
    tenant, _every_org, own = tasks
    build = client.post("/api/v1/pipeline/build", json={"build_core": False}, headers=admin_headers)
    assert build.status_code == 202, build.text

    assert tenant.get(f"/api/v1/tasks/{build.json()['task_id']}").status_code == 404
    assert [task["id"] for task in tenant.get("/api/v1/tasks").json()] == [own]


def test_an_admin_still_sees_every_task(client, admin_headers, tasks):
    _tenant, every_org, own = tasks

    listed = client.get("/api/v1/tasks", headers=admin_headers).json()

    assert {task["id"] for task in listed} == {every_org, own}


def test_a_platform_task_reader_without_sampler_read_still_sees_every_task(
    app, api_settings, tasks
):
    _tenant, every_org, own = tasks
    define_role(app, "task_auditor", ["task:read"], scoped=False)
    auditor = caller_in_group(app, api_settings, "auditor", roles=["task_auditor"], org_ids=[])

    listed = auditor.get("/api/v1/tasks").json()

    assert {task["id"] for task in listed} == {every_org, own}
    assert auditor.get(f"/api/v1/tasks/{every_org}").status_code == 200
