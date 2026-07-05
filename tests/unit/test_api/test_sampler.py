#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sampler.py
#  Purpose:      API contract tests for the sampler router
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Sampler API tests - unified submit/poll contract + RBAC.

Fast modes (recent/random) come back ``completed`` with the result inline; gated
modes (smart/anomaly) come back ``pending`` for polling. The ClickHouse client is
overridden with a fake so no real CH is needed.
"""

from __future__ import annotations

import pytest

from dfe_engine.api.deps import get_tenant_scoped_clickhouse_client
from dfe_engine.connections.tenant import TenantScopedClient


class _Result:
    def __init__(self, rows: list[str]) -> None:
        self.result_rows = [[r] for r in rows]


class FakeCH:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows
        self.last_settings: dict | None = None

    def query(self, sql, parameters=None, settings=None):
        self.last_settings = settings
        return _Result(self._rows)


@pytest.fixture
def fake_ch(app):
    """Override the per-principal CH client dependency with a fake.

    The sampler now acquires its client via ``get_tenant_scoped_clickhouse_client``
    (the acting user's fixed CH user) instead of the process-wide admin singleton,
    so the override targets that dependency.
    """
    app.dependency_overrides[get_tenant_scoped_clickhouse_client] = lambda: FakeCH(
        ['{"a": 1, "b": "x"}', '{"a": 2}']
    )
    yield
    app.dependency_overrides.pop(get_tenant_scoped_clickhouse_client, None)


def test_recent_sample_completes_inline(client, fake_ch, admin_headers):
    r = client.post(
        "/api/v1/sample",
        json={"mode": "recent", "table": "`db`.`events`", "limit": 5},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["task_id"]
    result = body["result"]
    assert result["count"] == 2
    assert result["rows"] == [{"a": 1, "b": "x"}, {"a": 2}]
    assert result["keys"] == ["a", "b"]


def test_poll_endpoint_returns_result(client, fake_ch, admin_headers):
    submit = client.post(
        "/api/v1/sample",
        json={"mode": "recent", "table": "`db`.`events`"},
        headers=admin_headers,
    ).json()
    got = client.get(f"/api/v1/samples/{submit['task_id']}", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["status"] == "completed"
    assert got.json()["result"]["count"] == 2


def test_smart_mode_returns_pending_task(client, fake_ch, admin_headers):
    # Gated mode is async by default (no inline wait) -> a task to poll.
    r = client.post(
        "/api/v1/sample",
        json={"mode": "smart", "table": "`db`.`events`"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"]
    assert body["status"] in {"pending", "running", "failed"}


def test_missing_target_is_400(client, fake_ch, admin_headers):
    r = client.post("/api/v1/sample", json={"mode": "recent"}, headers=admin_headers)
    assert r.status_code == 400
    assert "bad_sample_request" in r.text


def test_viewer_freeform_table_requires_query_raw(client, fake_ch, viewer_headers):
    """A viewer holds sampler:read but NOT query:raw. An explicit `table` is raw
    SQL on the shared admin client, so it must be walled off from viewers - else
    they read every org past any row policy (F-SAMPLER-SQLI)."""
    r = client.post(
        "/api/v1/sample",
        json={"mode": "recent", "table": "`db`.`events`"},
        headers=viewer_headers,
    )
    assert r.status_code == 403, r.text


def test_viewer_freeform_filter_requires_query_raw(client, fake_ch, viewer_headers):
    """The raw `filter` sink gets the same query:raw gate (F-SAMPLER-SQLI). The
    gate fires before target resolution, so filter-only (no table) is 403, not the
    400 a missing target would give."""
    r = client.post(
        "/api/v1/sample",
        json={"mode": "recent", "filter": "1=1 OR _org_id != ''"},
        headers=viewer_headers,
    )
    assert r.status_code == 403, r.text


def test_viewer_retains_sampler_read_without_freeform(client, fake_ch, viewer_headers):
    """Without a table/filter the query:raw gate never fires, so a viewer still
    passes the sampler:read RBAC check - a missing target is a 400 from inside the
    handler, NOT a 403. Proves the fix walls off ONLY the raw-SQL sink."""
    r = client.post("/api/v1/sample", json={"mode": "recent"}, headers=viewer_headers)
    assert r.status_code == 400, r.text
    assert "bad_sample_request" in r.text


def test_admin_freeform_table_allowed(client, fake_ch, admin_headers):
    """admin holds the '*' wildcard (so query:raw) - the raw-SQL sink stays open to
    a raw-SQL-authorised caller."""
    r = client.post(
        "/api/v1/sample",
        json={"mode": "recent", "table": "`db`.`events`"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text


def test_malformed_table_rejected(client, fake_ch, admin_headers):
    """Even a query:raw holder cannot smuggle a subselect through `table`: it must
    be a bare db.table identifier (F-SAMPLER-SQLI), so the injection is a 400."""
    r = client.post(
        "/api/v1/sample",
        json={
            "mode": "recent",
            "table": "(SELECT toString((*,)) AS _json FROM dfe_internal.repository) AS t",
        },
        headers=admin_headers,
    )
    assert r.status_code == 400, r.text
    assert "bad_sample_request" in r.text


def test_plain_db_table_passes(client, fake_ch, admin_headers):
    """A bare (unquoted) db.table override is a valid identifier and is accepted."""
    r = client.post(
        "/api/v1/sample",
        json={"mode": "recent", "table": "mydb.events"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


def test_unauthenticated_is_rejected(client, fake_ch):
    r = client.post("/api/v1/sample", json={"mode": "recent", "table": "`db`.`events`"})
    assert r.status_code in (401, 403)


def test_unknown_sample_task_is_404(client, admin_headers):
    r = client.get("/api/v1/samples/does-not-exist", headers=admin_headers)
    assert r.status_code == 404


def test_sample_routes_through_tenant_scoped_client(client, app, admin_headers):
    """A tenant-scoped principal's sample must inject DFE_current_tenant_id.

    Proves the sampler acquires its client via the per-principal dependency AND
    that a TenantScopedClient's tenant id (+ readonly settings-strip) reaches the
    CH query end-to-end. We drive it with admin headers (admin holds sampler:read)
    but override the dependency to hand back the tenant reader wrapper an
    org_analyst would resolve to - the endpoint code is identical regardless of
    which principal the dependency resolved.
    """
    recording = FakeCH(['{"a": 1}'])
    scoped = TenantScopedClient(recording, org_ids=["acme"], readonly=True)
    app.dependency_overrides[get_tenant_scoped_clickhouse_client] = lambda: scoped
    try:
        r = client.post(
            "/api/v1/sample",
            json={"mode": "recent", "table": "`db`.`events`"},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        # tenant id injected; the sampler's max_execution_time dropped (readonly)
        assert recording.last_settings == {"DFE_current_tenant_id": "acme"}
    finally:
        app.dependency_overrides.pop(get_tenant_scoped_clickhouse_client, None)


def test_poll_wrong_kind_task_is_404_not_500(client, app, admin_headers):
    """Polling a completed NON-sampler task id must 404, not 500. Without the
    kind guard, _to_response validates the foreign result as a SampleResult and
    raises a 500."""
    import dfe_engine.api.task_manager as tm

    t = tm._Task("wrong-kind-task", "hunt:execute")
    t.status = tm.TaskStatus.COMPLETED
    t.result = {"matches": 3}  # not a SampleResult shape
    app.state.task_manager._tasks[t.id] = t

    r = client.get(f"/api/v1/samples/{t.id}", headers=admin_headers)
    assert r.status_code == 404, r.text
