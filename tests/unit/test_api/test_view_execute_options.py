#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_view_execute_options.py
#  Purpose:      Options that cannot form a valid query are refused with 400
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST /api/v1/queries/views/{label}/execute answers a bad option with 400, not 500.

The view executor here has a client that cannot run a query, so a 400 proves the
request was refused before any SQL left the engine.
"""

import pytest

from dfe_engine.query.executor import ViewExecutor
from dfe_engine.query.models import ViewParameter
from tests.unit.test_query.view_fakes import ORG_ONLY, OneViewCatalog, view_definition

EXECUTE = "/api/v1/queries/views/analytics/events/execute"

_WITH_LIMIT = [
    *ORG_ONLY,
    ViewParameter(name="limit", clickhouse_type="UInt32", python_type="integer"),
]


class _NoQueryClient:
    def query(self, *args, **kwargs):
        raise AssertionError("the request reached ClickHouse")


def _install(app, params: list[ViewParameter]) -> None:
    app.state.view_executor = ViewExecutor(
        restricted_client=_NoQueryClient(),
        catalog=OneViewCatalog(view_definition(params)),
        database="dfe",
    )


@pytest.fixture
def refusing_executor(app, client):
    # After client, so the lifespan has finished setting app.state.
    _install(app, ORG_ONLY)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"after_key": "2024-01-15T12:00:00"}, "needs order_by"),
        ({"after_key": {"ts": 1}, "order_by": "ts"}, "string or a number"),
        ({"order_by": "ts) UNION SELECT 1 --"}, "invalid order_by"),
        ({"order_by": "ts", "after_key": "k", "offset": 10}, "cannot be combined"),
    ],
)
def test_a_bad_option_is_a_400(client, admin_headers, refusing_executor, options, message):
    resp = client.post(EXECUTE, json={"options": options}, headers=admin_headers)

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_options"
    assert message in resp.json()["message"]


def test_paging_a_view_that_caps_its_own_rows_is_a_400(app, client, admin_headers):
    _install(app, _WITH_LIMIT)

    resp = client.post(EXECUTE, json={"options": {"offset": 25}}, headers=admin_headers)

    assert resp.status_code == 400, resp.text
    assert "caps its own rows" in resp.json()["message"]
