#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_array_options.py
#  Purpose:      Array and object CLI options reach the API as the JSON the spec declares
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Option values serialise to the request body the OpenAPI schema declares.

Two layers. The first drives ``dfe sources from-catalogue`` through the real CLI
entry point against the in-process engine, so a 422 from the API fails the test.
The second builds a command tree from a one-route spec and reads the body an echo
server received, for every option shape the generator emits.
"""

import json
from typing import Any

import pytest
from click.testing import CliRunner
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from dfe_engine.cli.auto.app import build_root
from dfe_engine.cli.auto.build import GlobalOptions
from dfe_engine.cli.auto.client import Client
from dfe_engine.cli.auto.config import Credential, Store
from dfe_engine.cli.auto.errors import ExitCode

ENTRY_A = {"entry": "cisco-asa"}
ENTRY_B = {"entry": "okta", "name": "okta-prod", "archive": False}


def test_from_catalogue_creates_every_entry_given(harness):
    harness.seed_admin()

    result = harness.invoke(
        [
            "sources",
            "from-catalogue",
            "--entries",
            json.dumps(ENTRY_A),
            "--entries",
            json.dumps(ENTRY_B),
        ]
    )

    assert result.exit_code == 0, result.output
    reply = json.loads(result.output)
    assert reply["failed"] == []
    assert [row["source"] for row in reply["created"]] == ["cisco-asa", "okta-prod"]
    listed = harness.invoke(["--format", "json", "sources", "list"])
    stored = {row["name"] for row in json.loads(listed.output)}
    assert {"cisco-asa", "okta-prod"} <= stored


def test_from_catalogue_rejects_a_malformed_entry_before_sending(harness):
    harness.seed_admin()

    result = harness.invoke(["sources", "from-catalogue", "--entries", '{"entry": '])

    assert result.exit_code == ExitCode.VALIDATION
    assert "invalid JSON for entries" in result.output


def _spec(props: dict[str, Any]) -> dict[str, Any]:
    """A one-route OpenAPI document whose POST body has *props*."""
    return {
        "components": {
            "schemas": {
                "Thing": {"type": "object", "properties": props},
                "Pair": {"type": "object", "properties": {"k": {"type": "string"}}},
            }
        },
        "paths": {
            "/api/v1/things": {
                "post": {
                    "summary": "Make a thing",
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}
                        }
                    },
                    "responses": {"200": {"content": {"application/json": {"schema": {}}}}},
                }
            }
        },
    }


def _optional(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


_STRINGS = {"type": "array", "items": {"type": "string"}}
_PAIRS = {"type": "array", "items": {"$ref": "#/components/schemas/Pair"}}

# (id, prop schema, argv, JSON the API must receive for "prop")
SHAPES = [
    ("string", {"type": "string"}, ["--prop", "abc"], "abc"),
    ("int", {"type": "integer"}, ["--prop", "7"], 7),
    ("float", {"type": "number"}, ["--prop", "1.5"], 1.5),
    ("bool_on", {"type": "boolean", "default": False}, ["--prop"], True),
    ("bool_off", {"type": "boolean", "default": True}, ["--no-prop"], False),
    ("nested_object", {"type": "object"}, ["--prop", '{"a": {"b": [1, 2]}}'], {"a": {"b": [1, 2]}}),
    ("array_of_strings", _STRINGS, ["--prop", "a", "--prop", "b"], ["a", "b"]),
    ("optional_array_of_strings", _optional(_STRINGS), ["--prop", "a"], ["a"]),
    (
        "array_of_ints",
        {"type": "array", "items": {"type": "integer"}},
        ["--prop", "1", "--prop", "22"],
        [1, 22],
    ),
    (
        "array_of_floats",
        {"type": "array", "items": {"type": "number"}},
        ["--prop", "1.5", "--prop", "2"],
        [1.5, 2.0],
    ),
    (
        "array_of_bools",
        {"type": "array", "items": {"type": "boolean"}},
        ["--prop", "true", "--prop", "false"],
        [True, False],
    ),
    (
        "array_of_objects",
        _PAIRS,
        ["--prop", '{"k": "1"}', "--prop", '{"k": "2"}'],
        [{"k": "1"}, {"k": "2"}],
    ),
    ("optional_array_of_objects", _optional(_PAIRS), ["--prop", '{"k": "1"}'], [{"k": "1"}]),
    (
        "array_of_free_objects",
        {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        ["--prop", '{"x": {"y": 1}}'],
        [{"x": {"y": 1}}],
    ),
    (
        "array_of_arrays",
        {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        ["--prop", "[1, 2]", "--prop", "[3]"],
        [[1, 2], [3]],
    ),
    (
        "array_of_string_enum",
        {"type": "array", "items": {"type": "string", "enum": ["http", "grpc"]}},
        ["--prop", "http", "--prop", "grpc"],
        ["http", "grpc"],
    ),
    (
        "array_of_objects_keeps_a_plain_string",
        _PAIRS,
        ["--prop", "just-a-name"],
        ["just-a-name"],
    ),
]


async def _echo(request):
    return JSONResponse({"body": json.loads(await request.body() or "null")})


@pytest.fixture
def sent_body(tmp_path):
    """Run argv through a command tree built from a spec, return the body the server got."""
    app = Starlette(routes=[Route("/{rest:path}", _echo, methods=["POST"])])
    with TestClient(app) as tc:
        store = Store(home=tmp_path / "cfg")
        store.set_value("url", "http://test", "default")
        store.set_value("account", "admin", "default")
        store.save_credential(Credential(account="admin", token="t"))
        store.set_active_profile("default")
        opts = GlobalOptions(
            store=store, client_factory=lambda url, cred: Client(url, cred, session=tc)
        )

        def run(props: dict[str, Any], argv: list[str]) -> Any:
            root = build_root(spec=_spec(props))
            result = CliRunner().invoke(
                root,
                ["--format", "json", "things", *argv],
                obj=opts,
                catch_exceptions=False,
            )
            assert result.exit_code == 0, result.output
            return json.loads(result.output)["body"]

        yield run


@pytest.mark.parametrize(
    ("schema", "argv", "expected"),
    [case[1:] for case in SHAPES],
    ids=[case[0] for case in SHAPES],
)
def test_option_shape_serialises_to_its_schema_type(sent_body, schema, argv, expected):
    assert sent_body({"prop": schema}, argv) == {"prop": expected}


@pytest.mark.parametrize(
    "schema",
    [_STRINGS, _optional(_STRINGS), _PAIRS],
    ids=["strings", "optional_strings", "objects"],
)
def test_an_array_option_left_off_is_not_sent(sent_body, schema):
    """An update that names one field must not blank the arrays it never mentioned."""
    assert sent_body({"prop": schema, "other": {"type": "string"}}, ["--other", "x"]) == {
        "other": "x"
    }
