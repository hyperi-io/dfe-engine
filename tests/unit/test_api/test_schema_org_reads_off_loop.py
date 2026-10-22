#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_schema_org_reads_off_loop.py
#  Purpose:      The schema routes that hold a caller to its orgs read the org registry off the loop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A stalled org registry read holds up only the schema request waiting on it.

``json-paths``, ``sample-rows`` and ``promote-field`` bind a caller without a platform grant to
its orgs' tenant ids, which they find by reading the org registry. Each case stalls that read,
then asks ``/livez`` for an answer while it is stalled. A route that read on the event loop
holds ``/livez`` up with it, as it would every request and the probes.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, get_clickhouse_client
from dfe_engine.settings import DFESettings
from tests.support.accounts import admin_on_its_own_password
from tests.support.held_callers import caller_in_group, define_role
from tests.support.stalled_call import Stall, registry_call, request_while_stalled
from tests.unit.test_api.test_json_promotion import (
    NOMETA_SOURCE,
    PROMO_SOURCE,
    make_api_settings,
)
from tests.unit.test_api.test_schema_rows_org_scope import _Rows

SCHEMAS = "/api/v1/schemas"

# The request that waits on the stalled read.
_CASES: dict[str, tuple[str, str, dict | None, dict | None]] = {
    "discover JSON paths": ("GET", f"{SCHEMAS}/{NOMETA_SOURCE}/json-paths", None, None),
    "sample rows": ("GET", f"{SCHEMAS}/{NOMETA_SOURCE}/sample-rows", None, None),
    "promote a field": (
        "POST",
        f"{SCHEMAS}/{PROMO_SOURCE}/promote-field",
        {"dry_run": True},
        {"json_path": "user.id"},
    ),
}


@pytest.fixture
def settings(tmp_path) -> DFESettings:
    return make_api_settings(tmp_path)


@pytest.fixture
def app(settings):
    application = create_app(settings)
    application.dependency_overrides[get_clickhouse_client] = _Rows
    try:
        yield application
    finally:
        _registries.clear()


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        admin_on_its_own_password(app)
        yield c


@pytest.fixture
def tenant(app, client, settings) -> dict[str, str]:
    """Headers of a caller held to the org acme, whose scoped role holds schema:read and schema:write."""
    app.state.org_registry.create("acme", org_ids=["t-acme"])
    define_role(app, "tenant_schema", ["schema:read", "schema:write"], scoped=True)
    caller = caller_in_group(app, settings, "tenant", roles=["tenant_schema"], org_ids=["acme"])
    return {"Authorization": caller.headers["Authorization"]}


@pytest.mark.parametrize("case", list(_CASES))
def test_an_org_read_holds_up_only_its_own_schema_request(case, client, app, tenant, monkeypatch):
    method, path, params, body = _CASES[case]
    stall = Stall()
    registry_call("list")(app, stall, monkeypatch)

    done = request_while_stalled(
        client, stall, case, method, path, params=params, json=body, headers=tenant
    )

    assert done.status_code < 500, done.text
