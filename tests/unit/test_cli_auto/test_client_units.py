#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_client_units.py
#  Purpose:      Request construction + dispatch for the CLI HTTP client
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``client.Client`` request construction - unit AND against a REAL stub server.

No mocks: the dispatch tests stand up a tiny real Starlette app that echoes the
method / path / query / headers / body back as JSON, served through a Starlette
``TestClient`` (a real sync httpx client), and inject it into ``Client`` via the
same ``session`` seam the engine-backed tests use. The pure builders
(``build_url`` / ``build_query`` / ``build_body`` / ``_auth_headers``) are asserted
directly for the path-encoding, None-filtering, JSON-string parsing and auth-header
branches that the end-to-end orgs round-trips do not pin.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from dfe_engine.cli.auto.client import Client
from dfe_engine.cli.auto.config import Credential
from dfe_engine.cli.auto.errors import DfeCliError, ExitCode
from dfe_engine.cli.auto.spec import BodyProp, Operation, Param


def _op(**over) -> Operation:
    """An Operation with sane defaults; override only what a test cares about."""
    base = {
        "path": "/api/v1/things",
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


# --- real echo stub server ---------------------------------------------------


async def _echo(request):
    raw = await request.body()
    body = None
    if raw:
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode()
    return JSONResponse(
        {
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": body,
        }
    )


async def _empty(request):
    return Response(status_code=204)


async def _text(request):
    return PlainTextResponse("hello", media_type="text/plain")


@pytest.fixture
def echo_client():
    app = Starlette(
        routes=[
            Route("/api/v1/empty", _empty, methods=["GET"]),
            Route("/api/v1/text", _text, methods=["GET"]),
            Route("/{rest:path}", _echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ]
    )
    with TestClient(app) as tc:
        yield tc


# --- build_url ---------------------------------------------------------------


def test_build_url_substitutes_and_encodes_path_params():
    op = _op(path="/api/v1/things/{id}", path_params=[Param("id", "id", "str", True)])
    client = Client("http://testserver", None)
    try:
        # A value with a space + slash must be percent-encoded (safe="").
        assert client.build_url(op, {"id": "a b/c"}) == "/api/v1/things/a%20b%2Fc"
    finally:
        client.close()


# --- build_query -------------------------------------------------------------


def test_build_query_keeps_declared_nonnull_only():
    op = _op(
        query_params=[
            Param("q", "q", "str", False),
            Param("flag", "flag", "bool", False),
        ]
    )
    client = Client("http://testserver", None)
    try:
        # `q` set, `flag` None -> dropped; an undeclared key is ignored.
        out = client.build_query(op, {"q": "hi", "flag": None, "extra": "x"})
        assert out == {"q": "hi"}
    finally:
        client.close()


# --- build_body --------------------------------------------------------------


def test_build_body_parses_object_and_lists_arrays():
    op = _op(
        method="post",
        verb="create",
        body_props=[
            BodyProp("name", "name", "str", True),
            BodyProp("labels", "labels", "array", False),
            BodyProp("meta", "meta", "object", False),
        ],
    )
    client = Client("http://testserver", None)
    try:
        body = client.build_body(op, {"name": "x", "labels": ("a", "b"), "meta": '{"k": 1}'})
        assert body == {"name": "x", "labels": ["a", "b"], "meta": {"k": 1}}
    finally:
        client.close()


def test_build_body_invalid_json_object_raises_validation_error():
    op = _op(
        method="post",
        verb="create",
        body_props=[BodyProp("meta", "meta", "object", False)],
    )
    client = Client("http://testserver", None)
    try:
        with pytest.raises(DfeCliError) as excinfo:
            client.build_body(op, {"meta": "{not json"})
        assert excinfo.value.exit_code == int(ExitCode.VALIDATION)
    finally:
        client.close()


def test_build_body_freeform_variants():
    op = _op(method="post", verb="create", path="/api/v1/blobs", freeform_body=True)
    client = Client("http://testserver", None)
    try:
        assert client.build_body(op, {"body": '{"a": 1}'}) == {"a": 1}
        assert client.build_body(op, {"body": None}) is None
        # An already-parsed object is passed straight through.
        assert client.build_body(op, {"body": {"a": 1}}) == {"a": 1}
    finally:
        client.close()


# --- _auth_headers -----------------------------------------------------------


def test_auth_headers_api_key_vs_bearer_vs_none():
    assert Client("http://t", Credential("acct", api_key="K"))._auth_headers() == {"X-API-Key": "K"}
    assert Client("http://t", Credential("acct", token="T"))._auth_headers() == {
        "Authorization": "Bearer T"
    }
    assert Client("http://t", None)._auth_headers() == {}
    # A credential with neither token nor api_key contributes no header.
    assert Client("http://t", Credential("acct"))._auth_headers() == {}


# --- real dispatch (end to end through the stub) -----------------------------


def test_get_dispatch_sends_query_and_api_key_header(echo_client):
    op = _op(
        path="/api/v1/things/{id}",
        verb="describe",
        path_params=[Param("id", "id", "str", True)],
        query_params=[Param("q", "q", "str", False)],
    )
    client = Client("http://testserver", Credential("acct", api_key="K"), session=echo_client)
    echoed = client.call_json(op, path_args={"id": "42"}, query_args={"q": "hi"})
    assert echoed["method"] == "GET"
    assert echoed["path"] == "/api/v1/things/42"
    assert echoed["query"] == {"q": "hi"}
    assert echoed["headers"]["x-api-key"] == "K"
    # A GET carries no request body.
    assert echoed["body"] is None


def test_post_dispatch_sends_assembled_body_and_bearer(echo_client):
    op = _op(
        path="/api/v1/things",
        method="post",
        verb="create",
        body_props=[
            BodyProp("name", "name", "str", True),
            BodyProp("labels", "labels", "array", False),
        ],
    )
    client = Client("http://testserver", Credential("acct", token="T"), session=echo_client)
    echoed = client.call_json(op, body_args={"name": "n", "labels": ("a", "b")})
    assert echoed["method"] == "POST"
    assert echoed["body"] == {"name": "n", "labels": ["a", "b"]}
    assert echoed["headers"]["authorization"] == "Bearer T"


def test_call_json_204_returns_none(echo_client):
    op = _op(path="/api/v1/empty", verb="describe", group_path=["empty"])
    client = Client("http://testserver", None, session=echo_client)
    assert client.call_json(op) is None


def test_call_json_non_json_returns_text(echo_client):
    op = _op(path="/api/v1/text", verb="describe", group_path=["text"])
    client = Client("http://testserver", None, session=echo_client)
    assert client.call_json(op) == "hello"
