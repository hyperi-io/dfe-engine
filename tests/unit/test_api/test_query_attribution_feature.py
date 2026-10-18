#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_query_attribution_feature.py
#  Purpose:      A request's ClickHouse attribution names its route template, never its path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The ``feature`` every ClickHouse query of a v1 request is tagged with.

The tag lands in ``system.query_log.log_comment`` and the query-log archive, so a
login in the request path would be stored with every query the request made. The
route reads the tag back from inside the request, as the ClickHouse wrapper does.
"""

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.v1 import _attribution_scope
from dfe_engine.clickhouse.attribution import current_tags


def test_the_feature_is_the_route_template_without_the_login():
    accounts = APIRouter()

    @accounts.get("/accounts/{username}")
    async def account(username: str) -> dict[str, str | None]:
        return {"feature": current_tags().feature}

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(_attribution_scope)])
    v1.include_router(accounts)
    app = FastAPI()
    app.include_router(v1, prefix="/api")

    with TestClient(app) as client:
        feature = client.get("/api/v1/accounts/jane.doe@example.com").json()["feature"]

    assert feature == "/api/v1/accounts/{username}"
