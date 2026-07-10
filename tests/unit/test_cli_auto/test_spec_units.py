#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_spec_units.py
#  Purpose:      Unit tests for spec parsing + the x-cli exposure extension
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pure-unit coverage of ``spec.iter_operations`` + ``cli_exposure.cli_enabled``.

Drives a small hand-built OpenAPI document (no engine needed) so the parser's
sharp edges are pinned directly: the ``x-cli`` opt-out, ``$ref`` resolution and its
self-referential guard, path/query param splitting, ``allOf`` request-body merging,
the raw-object ``freeform`` body, and the ``PaginatedResponse_*`` response-ref that
the paginator keys on. ``test_tree_naming`` covers the same code against the LIVE
spec; this fixes the behaviour on inputs the live spec may not exercise.
"""

from __future__ import annotations

from dfe_engine.api.cli_exposure import cli_enabled
from dfe_engine.cli.auto.spec import RefResolver, iter_operations


def _spec() -> dict:
    return {
        "components": {
            "schemas": {
                "Widget": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                },
                "WidgetBase": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                "WidgetExtra": {
                    "type": "object",
                    "properties": {"size": {"type": "integer", "default": 1}},
                },
                "WidgetCreate": {
                    "allOf": [
                        {"$ref": "#/components/schemas/WidgetBase"},
                        {"$ref": "#/components/schemas/WidgetExtra"},
                    ]
                },
                "Node": {"$ref": "#/components/schemas/Node"},  # self-referential
                "PaginatedResponse_Widget_": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array"},
                        "page": {"type": "integer"},
                        "per_page": {"type": "integer"},
                        "next_page": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    },
                },
            }
        },
        "paths": {
            "/api/v1/widgets": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/PaginatedResponse_Widget_"
                                    }
                                }
                            }
                        }
                    }
                },
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/WidgetCreate"}
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Widget"}
                                }
                            }
                        }
                    },
                },
            },
            "/api/v1/widgets/{widget_id}": {
                "get": {
                    "parameters": [
                        {
                            "name": "widget_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "expand",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean", "default": False},
                        },
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Widget"}
                                }
                            }
                        }
                    },
                },
                "delete": {  # opted out of the CLI
                    "x-cli": {"enabled": False},
                    "responses": {"204": {"description": "gone"}},
                },
            },
            "/api/v1/blobs": {
                "post": {  # raw-object body -> a single --body JSON option
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "object"}}}
                    },
                    "responses": {
                        "200": {"content": {"application/json": {"schema": {"type": "object"}}}}
                    },
                },
            },
        },
    }


def _by_key(spec: dict) -> dict[tuple, object]:
    return {(tuple(op.group_path), op.verb): op for op in iter_operations(spec)}


# --- cli_enabled (x-cli extension) -------------------------------------------


def test_cli_enabled_default_on():
    assert cli_enabled({}) is True
    assert cli_enabled({"summary": "no x-cli block"}) is True


def test_cli_enabled_explicit_opt_out():
    assert cli_enabled({"x-cli": {"enabled": False}}) is False


def test_cli_enabled_explicit_opt_in_and_empty_block():
    assert cli_enabled({"x-cli": {"enabled": True}}) is True
    # An x-cli block that omits `enabled` still defaults on.
    assert cli_enabled({"x-cli": {}}) is True


def test_cli_enabled_ignores_non_dict_extension():
    # A malformed (non-dict) x-cli value never hides an operation.
    assert cli_enabled({"x-cli": "nope"}) is True


# --- RefResolver -------------------------------------------------------------


def test_ref_resolver_resolves_and_names():
    resolver = RefResolver(_spec())
    resolved = resolver.resolve({"$ref": "#/components/schemas/Widget"})
    assert resolved["properties"]["name"] == {"type": "string"}
    assert resolver.ref_name({"$ref": "#/components/schemas/Widget"}) == "Widget"
    assert resolver.ref_name({"type": "object"}) is None


def test_ref_resolver_missing_and_non_dict_are_empty():
    resolver = RefResolver(_spec())
    assert resolver.resolve({"$ref": "#/components/schemas/DoesNotExist"}) == {}
    assert resolver.resolve(None) == {}
    # A non-ref schema passes through unchanged.
    assert resolver.resolve({"type": "string"}) == {"type": "string"}


def test_ref_resolver_self_reference_guarded():
    # A schema that $refs itself must not recurse forever.
    resolver = RefResolver(_spec())
    assert resolver.resolve({"$ref": "#/components/schemas/Node"}) == {}


# --- iter_operations ---------------------------------------------------------


def test_hidden_delete_operation_is_dropped():
    ops = _by_key(_spec())
    assert (("widgets",), "delete") not in ops
    # ... while its sibling describe (GET item) survives.
    assert (("widgets",), "describe") in ops


def test_list_op_carries_pagination_ref():
    op = _by_key(_spec())[(("widgets",), "list")]
    assert op.response_ref_name == "PaginatedResponse_Widget_"


def test_path_and_query_params_split_and_typed():
    op = _by_key(_spec())[(("widgets",), "describe")]
    assert [p.name for p in op.path_params] == ["widget_id"]
    assert op.path_params[0].required is True
    assert [p.name for p in op.query_params] == ["expand"]
    assert op.query_params[0].type == "bool"


def test_allof_body_is_merged_into_props():
    op = _by_key(_spec())[(("widgets",), "create")]
    assert op.freeform_body is False
    by_name = {p.name: p for p in op.body_props}
    assert set(by_name) == {"name", "size"}
    # required propagates from WidgetBase; size is an int with the schema default.
    assert by_name["name"].required is True
    assert by_name["size"].required is False
    assert by_name["size"].type == "int"
    assert by_name["size"].default == 1


def test_raw_object_body_is_freeform():
    # Looked up by path: a raw-object body op with no item-sibling route derives as
    # a root action verb, so the freeform assertion keys off the path, not the verb.
    op = next(o for o in iter_operations(_spec()) if o.path == "/api/v1/blobs")
    assert op.freeform_body is True
    assert op.body_props == []
