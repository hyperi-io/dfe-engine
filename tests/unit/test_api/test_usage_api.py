#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_usage_api.py
#  Purpose:      The usage routes' shapes, statuses and who reads which org's numbers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The usage API over a stand-in for the usage tables.

The stand-in answers each read with the rows set for its metric, dated back from the
``last_day`` the route binds, so no test depends on the clock. What the SQL itself
answers is tests/integration/test_usage_ch.py.
"""

from datetime import date, timedelta

import pytest
from dfe_schemas.usage import (
    ALL_ORGS,
    CORES_DEPLOYED,
    EVENTS_PER_SECOND,
    INGESTED_BYTES,
    NODE_CORES,
)

from dfe_engine.api.deps import get_clickhouse_client
from dfe_engine.api.v1.usage import HIGHEST_LABEL, METRIC_LABELS, OVERVIEW_DAYS
from tests.support.held_callers import caller_in_group, define_role

ACME_TENANTS = ["t-acme-1", "t-acme-2"]


class _Result:
    def __init__(self, rows: list[tuple]) -> None:
        self.result_rows = rows


class _UsageTables:
    """Answers a usage read with ``(days back from last_day, value)`` rows for its metric."""

    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[int, float]]] = {}
        self.reads: list[tuple[str, dict]] = []
        self.fail = False

    def query(self, sql: str, parameters=None, settings=None) -> _Result:
        params = dict(parameters or {})
        self.reads.append((sql, params))
        if self.fail:
            raise RuntimeError("clickhouse is down at 10.0.0.9")
        last = params["last_day"]
        rows = [
            (last - timedelta(days=back), value)
            for back, value in self.rows.get(params["metric"], [])
        ]
        return _Result(sorted(rows))

    def metrics_read(self) -> list[str]:
        return [params["metric"] for _, params in self.reads]

    def scopes_read(self) -> list[list[str]]:
        return [params["scopes"] for _, params in self.reads]


@pytest.fixture
def tables(app):
    ch = _UsageTables()
    app.dependency_overrides[get_clickhouse_client] = lambda: ch
    yield ch
    app.dependency_overrides.pop(get_clickhouse_client, None)


@pytest.fixture
def acme(app, client):
    """The org ``acme``, whose rows carry two tenant ids that are not its name, and ``beta``."""
    app.state.org_registry.create("acme", org_ids=ACME_TENANTS)
    app.state.org_registry.create("beta")


@pytest.fixture
def tenant(app, api_settings, acme):
    """An org_viewer of acme: held to acme's tenant ids."""
    return caller_in_group(app, api_settings, "acme-viewer", roles=["org_viewer"], org_ids=["acme"])


def _by_metric(body: dict) -> dict[str, dict]:
    return {entry["metric"]: entry for entry in body["metrics"]}


# -- the overview --------------------------------------------------------------


def test_a_platform_caller_reads_every_metric_for_the_whole_deployment(
    client, admin_headers, tables
):
    tables.rows = {
        CORES_DEPLOYED: [(10, 9.0), (3, 4.0), (1, 6.5), (0, 2.0)],
        NODE_CORES: [(0, 48.0)],
        INGESTED_BYTES: [(2, 1.5e12)],
    }

    resp = client.get("/api/v1/usage/overview", headers=admin_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    last = date.fromisoformat(body["to"])
    assert date.fromisoformat(body["from"]) == last - timedelta(days=OVERVIEW_DAYS - 1)
    assert (body["orgs"], body["highest_7d_label"]) == ([], HIGHEST_LABEL)
    assert [entry["metric"] for entry in body["metrics"]] == [
        CORES_DEPLOYED,
        NODE_CORES,
        INGESTED_BYTES,
        EVENTS_PER_SECOND,
    ]
    cores = _by_metric(body)[CORES_DEPLOYED]
    assert cores == {
        "metric": CORES_DEPLOYED,
        "label": METRIC_LABELS[CORES_DEPLOYED],
        "unit": "cores",
        "status": "measured",
        # 9.0 is ten days back, outside the seven.
        "highest_7d": {"day": str(last - timedelta(days=1)), "value": 6.5},
        "latest": {"day": str(last), "value": 2.0},
        "series": [
            {"day": str(last - timedelta(days=back)), "value": value}
            for back, value in [(10, 9.0), (3, 4.0), (1, 6.5), (0, 2.0)]
        ],
    }
    assert set(map(tuple, tables.scopes_read())) == {(ALL_ORGS,)}


def test_a_metric_with_no_rows_is_not_measured_never_zero(client, admin_headers, tables):
    resp = client.get("/api/v1/usage/overview", headers=admin_headers)

    assert resp.status_code == 200, resp.text
    for entry in resp.json()["metrics"]:
        assert (entry["status"], entry["highest_7d"], entry["latest"], entry["series"]) == (
            "not_measured",
            None,
            None,
            [],
        ), entry["metric"]


def test_a_metric_measured_only_before_the_seven_days_has_no_highest(client, admin_headers, tables):
    tables.rows = {INGESTED_BYTES: [(8, 100.0)]}

    entry = _by_metric(client.get("/api/v1/usage/overview", headers=admin_headers).json())[
        INGESTED_BYTES
    ]

    assert (entry["status"], entry["highest_7d"], entry["latest"]["value"]) == (
        "measured",
        None,
        100.0,
    )


def test_a_held_caller_reads_its_orgs_bytes_and_events_and_no_core_count(tenant, tables):
    tables.rows = {INGESTED_BYTES: [(0, 50.0)], EVENTS_PER_SECOND: [(0, 3.0)]}

    resp = tenant.get("/api/v1/usage/overview")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["orgs"] == ["acme"]
    metrics = _by_metric(body)
    for withheld in (CORES_DEPLOYED, NODE_CORES):
        assert (metrics[withheld]["status"], metrics[withheld]["series"]) == ("not_permitted", [])
    assert metrics[INGESTED_BYTES]["status"] == "measured"
    assert metrics[EVENTS_PER_SECOND]["latest"]["value"] == 3.0
    # The withheld metrics are never read, and nothing is read for the whole deployment.
    assert tables.metrics_read() == [INGESTED_BYTES, EVENTS_PER_SECOND]
    assert tables.scopes_read() == [ACME_TENANTS, ACME_TENANTS]


def test_a_held_caller_whose_orgs_resolve_to_no_registered_org_is_refused(
    app, api_settings, acme, tables
):
    stray = caller_in_group(app, api_settings, "stray", roles=["org_viewer"], org_ids=["t-nobody"])

    resp = stray.get("/api/v1/usage/overview")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "no_org"
    assert tables.reads == []


def test_a_scoped_custom_role_is_held_and_an_unscoped_one_is_not(app, api_settings, acme, tables):
    define_role(app, "usage_tenant", ["usage:read"], scoped=True)
    define_role(app, "usage_platform", ["usage:read"], scoped=False)
    held = caller_in_group(app, api_settings, "ut", roles=["usage_tenant"], org_ids=["acme"])
    platform = caller_in_group(app, api_settings, "up", roles=["usage_platform"], org_ids=["acme"])

    held_body = held.get("/api/v1/usage/overview").json()
    platform_body = platform.get("/api/v1/usage/overview").json()

    assert (held_body["orgs"], platform_body["orgs"]) == (["acme"], [])
    assert _by_metric(platform_body)[CORES_DEPLOYED]["status"] == "not_measured"
    assert _by_metric(held_body)[CORES_DEPLOYED]["status"] == "not_permitted"


def test_a_caller_without_usage_read_is_refused(app, api_settings, acme, tables):
    viewer = caller_in_group(app, api_settings, "dv", roles=["data_viewer"], org_ids=[])

    resp = viewer.get("/api/v1/usage/overview")

    assert resp.status_code == 403, resp.text
    assert tables.reads == []


def test_an_anonymous_caller_is_refused(client, tables):
    assert client.get("/api/v1/usage/overview").status_code == 401
    assert client.get("/api/v1/usage", params={"metric": INGESTED_BYTES}).status_code == 401


@pytest.mark.parametrize(
    "role", ["infra_admin", "infra_viewer", "dfe_operator", "org_viewer", "admin"]
)
def test_the_roles_that_hold_usage_read(app, api_settings, acme, tables, role):
    caller = caller_in_group(app, api_settings, f"r-{role}", roles=[role], org_ids=["acme"])

    assert caller.get("/api/v1/usage/overview").status_code == 200


# -- a role bound at an org's scope --------------------------------------------


@pytest.fixture
def org_bound(app, api_settings, acme):
    """An org_viewer whose group is bound at acme's scope, as a deployment binds a tenant."""
    return caller_in_group(
        app, api_settings, "acme-bound", roles=["org_viewer"], org_ids=["acme"], scope="org:acme"
    )


def test_an_org_bound_viewer_reads_its_own_orgs_overview(org_bound, tables):
    tables.rows = {INGESTED_BYTES: [(0, 50.0)], EVENTS_PER_SECOND: [(0, 3.0)]}

    resp = org_bound.get("/api/v1/usage/overview")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["orgs"] == ["acme"]
    metrics = _by_metric(body)
    for withheld in (CORES_DEPLOYED, NODE_CORES):
        assert (metrics[withheld]["status"], metrics[withheld]["series"]) == ("not_permitted", [])
    assert metrics[INGESTED_BYTES]["status"] == "measured"
    assert tables.metrics_read() == [INGESTED_BYTES, EVENTS_PER_SECOND]
    assert tables.scopes_read() == [ACME_TENANTS, ACME_TENANTS]


def test_an_org_bound_viewer_reads_its_own_orgs_series(org_bound, tables):
    tables.rows = {INGESTED_BYTES: [(0, 5.0)]}

    resp = _usage(org_bound, metric=INGESTED_BYTES)
    cores = _usage(org_bound, metric=CORES_DEPLOYED)

    assert resp.status_code == 200, resp.text
    assert (resp.json()["orgs"], resp.json()["status"]) == (["acme"], "measured")
    assert cores.status_code == 200, cores.text
    assert cores.json()["status"] == "not_permitted"
    assert tables.scopes_read() == [ACME_TENANTS]


def test_an_org_without_usage_read_is_never_read_for_a_caller_holding_it_elsewhere(
    app, api_settings, acme, tables
):
    """usage:read bound at acme reads acme, never beta, where the caller holds only data_viewer."""
    app.state.group_store.create("beta-dv", roles=["data_viewer"], scope="org:beta")
    app.state.group_store.update("beta-dv", org_ids=["beta"])
    caller = caller_in_group(
        app, api_settings, "split", roles=["org_viewer"], org_ids=["acme"], scope="org:acme"
    )
    app.state.account_store.update("split", groups=["split-group", "beta-dv"])
    app.state.group_store.add_member("beta-dv", "split")

    overview = caller.get("/api/v1/usage/overview")
    named = _usage(caller, metric=INGESTED_BYTES, org="beta")

    assert overview.status_code == 200, overview.text
    assert overview.json()["orgs"] == ["acme"]
    assert set(map(tuple, tables.scopes_read())) == {tuple(ACME_TENANTS)}
    assert named.status_code == 403, named.text
    assert named.json()["code"] == "org_not_granted"


def test_an_org_bound_caller_without_usage_read_is_refused(app, api_settings, acme, tables):
    viewer = caller_in_group(
        app, api_settings, "acme-dv", roles=["data_viewer"], org_ids=["acme"], scope="org:acme"
    )

    overview = viewer.get("/api/v1/usage/overview")
    series = _usage(viewer, metric=INGESTED_BYTES)

    assert (overview.status_code, series.status_code) == (403, 403), overview.text
    assert tables.reads == []


def test_a_failed_read_is_a_503_that_keeps_the_backend_text_in_the_log(
    client, admin_headers, tables
):
    tables.fail = True

    resp = client.get("/api/v1/usage/overview", headers=admin_headers)

    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "usage_unavailable"
    assert "10.0.0.9" not in resp.text


# -- one metric over a range ---------------------------------------------------


def _usage(caller, headers=None, **params):
    return caller.get("/api/v1/usage", params=params, headers=headers or {})


def test_a_range_reads_the_days_asked_for(client, admin_headers, tables):
    tables.rows = {INGESTED_BYTES: [(5, 7.0), (2, 9.0), (0, 1.0)]}

    resp = _usage(
        client,
        admin_headers,
        metric=INGESTED_BYTES,
        granularity="day",
        **{
            "from": "2026-09-01",
            "to": "2026-09-30",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {key: body[key] for key in ("metric", "unit", "granularity", "from", "to", "orgs")} == {
        "metric": INGESTED_BYTES,
        "unit": "bytes",
        "granularity": "day",
        "from": "2026-09-01",
        "to": "2026-09-30",
        "orgs": [],
    }
    assert body["highest"] == {"day": "2026-09-28", "value": 9.0}
    assert body["latest"] == {"day": "2026-09-30", "value": 1.0}
    [(_, params)] = tables.reads
    assert params["first_day"] == date(2026, 9, 1)
    assert params["scopes"] == [ALL_ORGS]


def test_a_range_defaults_to_the_last_thirty_days(client, admin_headers, tables):
    body = _usage(client, admin_headers, metric=INGESTED_BYTES).json()

    last = date.fromisoformat(body["to"])
    assert date.fromisoformat(body["from"]) == last - timedelta(days=OVERVIEW_DAYS - 1)
    assert body["status"] == "not_measured"


def test_a_platform_caller_may_name_one_org(client, admin_headers, acme, tables):
    tables.rows = {INGESTED_BYTES: [(0, 5.0)]}

    body = _usage(client, admin_headers, metric=INGESTED_BYTES, org="acme").json()

    assert (body["orgs"], body["status"]) == (["acme"], "measured")
    assert tables.scopes_read() == [ACME_TENANTS]


def test_an_org_declaring_no_tenant_ids_reads_its_name(client, admin_headers, acme, tables):
    _usage(client, admin_headers, metric=EVENTS_PER_SECOND, org="beta")

    assert tables.scopes_read() == [["beta"]]


def test_a_platform_caller_naming_an_unregistered_org_is_a_422(client, admin_headers, tables):
    resp = _usage(client, admin_headers, metric=INGESTED_BYTES, org="nope")

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "unknown_org"
    assert tables.reads == []


def test_a_core_count_per_org_is_a_422(client, admin_headers, acme, tables):
    resp = _usage(client, admin_headers, metric=CORES_DEPLOYED, org="acme")

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "not_per_org"


@pytest.mark.parametrize("org", ["beta", "nope"])
def test_a_held_caller_naming_an_org_it_does_not_hold_learns_nothing_about_it(tenant, tables, org):
    """A registered org and an unregistered one answer alike."""
    resp = _usage(tenant, metric=INGESTED_BYTES, org=org)

    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert (body["code"], body["message"]) == (
        "org_not_granted",
        f"The caller holds no org '{org}'",
    )
    assert tables.reads == []


def test_a_held_caller_may_name_its_own_org(tenant, tables):
    resp = _usage(tenant, metric=INGESTED_BYTES, org="acme")

    assert resp.status_code == 200, resp.text
    assert tables.scopes_read() == [ACME_TENANTS]


def test_a_held_callers_core_count_is_not_permitted_and_never_read(tenant, tables):
    resp = _usage(tenant, metric=NODE_CORES)

    assert resp.status_code == 200, resp.text
    assert (resp.json()["status"], resp.json()["series"]) == ("not_permitted", [])
    assert tables.reads == []


def test_a_held_caller_holding_two_orgs_reads_both(app, api_settings, acme, tables):
    both = caller_in_group(
        app, api_settings, "both", roles=["org_viewer"], org_ids=["acme", "beta"]
    )

    body = _usage(both, metric=INGESTED_BYTES).json()

    assert body["orgs"] == ["acme", "beta"]
    assert tables.scopes_read() == [sorted([*ACME_TENANTS, "beta"])]


def test_an_unknown_metric_is_a_422(client, admin_headers, tables):
    resp = _usage(client, admin_headers, metric="bytes_out")

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "unknown_metric"


def test_a_range_ending_before_it_starts_is_a_422(client, admin_headers, tables):
    resp = _usage(
        client, admin_headers, metric=INGESTED_BYTES, **{"from": "2026-10-02", "to": "2026-10-01"}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "bad_range"


@pytest.mark.parametrize(
    "params",
    [
        {"granularity": "hour"},
        {"from": "yesterday"},
        {"org": "has space"},
    ],
)
def test_a_malformed_parameter_is_a_422(client, admin_headers, tables, params):
    resp = _usage(client, admin_headers, metric=INGESTED_BYTES, **params)

    assert resp.status_code == 422, resp.text
    assert tables.reads == []


def test_the_metric_parameter_lists_the_metrics_in_the_spec(app):
    spec = app.openapi()

    parameters = spec["paths"]["/api/v1/usage"]["get"]["parameters"]
    (metric,) = [p for p in parameters if p["name"] == "metric"]
    assert metric["schema"]["enum"] == list(METRIC_LABELS)
    names = {p["name"] for p in parameters}
    assert {"from", "to", "org", "granularity"} <= names


def test_every_metric_dfe_schemas_declares_has_a_label():
    from dfe_schemas.usage import METRICS

    assert [metric.name for metric in METRICS] == list(METRIC_LABELS)


def test_a_system_bound_viewer_never_reads_an_org_only_another_group_names(
    app, api_settings, acme, tables
):
    """org_viewer at system scope over acme, plus data_viewer bound at beta: beta is never read."""
    app.state.group_store.create("beta-dv", roles=["data_viewer"], scope="org:beta")
    app.state.group_store.update("beta-dv", org_ids=["beta"])
    caller = caller_in_group(app, api_settings, "mixed", roles=["org_viewer"], org_ids=["acme"])
    app.state.account_store.update("mixed", groups=["mixed-group", "beta-dv"])
    app.state.group_store.add_member("beta-dv", "mixed")

    overview = caller.get("/api/v1/usage/overview")
    named = _usage(caller, metric=INGESTED_BYTES, org="beta")

    assert overview.status_code == 200, overview.text
    assert overview.json()["orgs"] == ["acme"]
    assert set(map(tuple, tables.scopes_read())) == {tuple(ACME_TENANTS)}
    assert named.status_code == 403, named.text
    assert named.json()["code"] == "org_not_granted"


def test_an_org_bound_group_naming_only_a_tenant_id_reads_its_org(app, api_settings, acme, tables):
    viewer = caller_in_group(
        app, api_settings, "by-tenant", roles=["org_viewer"], org_ids=["t-acme-1"], scope="org:acme"
    )

    resp = viewer.get("/api/v1/usage/overview")

    assert resp.status_code == 200, resp.text
    assert resp.json()["orgs"] == ["acme"]


def test_a_jit_shaped_context_naming_only_a_tenant_id_holds_its_org(app, acme):
    """The grants and org ids jit.py builds: an org-scoped grant, and only the tenant id."""
    from starlette.requests import Request

    from dfe_engine.api.deps import allowed_at_system_or_own_org, held_orgs
    from dfe_engine.auth.models import AuthContext, Scope, ScopedGrant

    request = Request({"type": "http", "app": app, "headers": []})
    user = AuthContext(
        user_id="jit",
        roles=["org_viewer"],
        grants=[ScopedGrant(role="org_viewer", scope=Scope(type="org", id="acme"))],
        org_ids=["t-acme-1"],
    )

    assert allowed_at_system_or_own_org(request, user, "usage:read")
    assert [org.name for org in held_orgs(request, user, "usage:read")] == ["acme"]
