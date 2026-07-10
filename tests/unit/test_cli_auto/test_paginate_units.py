#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_paginate_units.py
#  Purpose:      Pagination detection + auto-follow edge cases (paginate.py)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``paginate`` detection + the auto-follow robustness edges.

``test_roundtrip_pagination`` covers the happy auto-follow / --limit / --no-paginate
paths against the real engine. This pins the parts the engine will not produce: the
``is_paginated`` predicate (by response-ref AND by envelope shape), the
misbehaving-server guard (a ``next_page`` that does not advance must STOP, not spin),
and a non-envelope runtime body being returned as-is. A tiny real Starlette stub
returns the crafted bodies - no mocks.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from dfe_engine.cli.auto import paginate
from dfe_engine.cli.auto.client import Client
from dfe_engine.cli.auto.spec import Operation


def _op(path: str, **over) -> Operation:
    base = {
        "path": path,
        "method": "get",
        "path_params": [],
        "query_params": [],
        "body_props": [],
        "success_response_schema": None,
        "response_ref_name": None,
        "summary": "",
        "description": "",
        "group_path": ["things"],
        "verb": "list",
        "freeform_body": False,
    }
    base.update(over)
    return Operation(**base)


# --- is_paginated ------------------------------------------------------------


def test_is_paginated_by_response_ref():
    assert paginate.is_paginated(_op("/x", response_ref_name="PaginatedResponse_Org_"))


def test_is_paginated_by_envelope_shape():
    schema = {"properties": {"items": {}, "next_page": {}, "page": {}}}
    assert paginate.is_paginated(_op("/x", success_response_schema=schema))


def test_is_paginated_negative():
    assert not paginate.is_paginated(_op("/x", response_ref_name="Org"))
    assert not paginate.is_paginated(_op("/x", success_response_schema={"properties": {"id": {}}}))


# --- collect edges (real stub server) ----------------------------------------


async def _loop(request):
    # A misbehaving server: next_page never advances past the page just fetched.
    page = int(request.query_params.get("page", "1"))
    return JSONResponse({"items": [f"p{page}"], "page": page, "per_page": 1, "next_page": page})


async def _plainlist(request):
    # Not an envelope at all - a bare JSON array.
    return JSONResponse([1, 2, 3, 4])


@pytest.fixture
def stub():
    app = Starlette(
        routes=[
            Route("/api/v1/loop", _loop, methods=["GET"]),
            Route("/api/v1/plain", _plainlist, methods=["GET"]),
        ]
    )
    with TestClient(app) as tc:
        yield tc


def test_collect_stops_when_next_page_does_not_advance(stub, capsys):
    client = Client("http://testserver", None, session=stub)
    items = paginate.collect(client, _op("/api/v1/loop"), path_args={}, query_args={})
    # Exactly one page accumulated, then the guard stopped the walk.
    assert items == ["p1"]
    assert "pagination stopped" in capsys.readouterr().err


def test_collect_non_envelope_body_returned_with_limit(stub):
    client = Client("http://testserver", None, session=stub)
    got = paginate.collect(client, _op("/api/v1/plain"), path_args={}, query_args={}, limit=2)
    assert got == [1, 2]
