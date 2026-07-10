#  Project:      dfe-engine
#  File:         cli/auto/spec.py
#  Purpose:      Load the live OpenAPI spec + parse it into CLI Operation records
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Turn the engine OpenAPI spec (the SSoT) into a flat list of ``Operation``s.

The spec is read LIVE from ``create_app().openapi()`` so it is never stale versus
the committed ``openapi-spec/openapi.json``. Operations whose ``x-cli.enabled`` is
False are skipped. ``$ref`` pointers into ``#/components/schemas`` are resolved
here so the builder sees concrete request/response shapes.

Each operation carries its derived group path + verb (see ``naming``), the path /
query params, the flattened request-body props, and the resolved success-response
schema (plus its ref name, so the paginator can spot our ``PaginatedResponse_*``
envelope).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dfe_engine.api.cli_exposure import cli_enabled

from .naming import _kebab, api_segments, derive_group_and_verb, is_param

# HTTP methods we surface. (head/options/trace are never CLI commands.)
_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass
class Param:
    """A path or query parameter lifted from the spec."""

    name: str  # wire name (as the API expects it)
    cli_name: str  # kebab-cased option/argument name
    type: str  # str | int | float | bool | array | object
    required: bool
    description: str = ""
    default: Any = None
    enum: list[Any] | None = None


@dataclass
class BodyProp:
    """A single request-body property, flattened to one CLI option."""

    name: str  # wire name
    cli_name: str  # kebab-cased option name
    type: str  # str | int | float | bool | array | object
    required: bool
    description: str = ""
    default: Any = None
    enum: list[Any] | None = None


@dataclass
class Operation:
    """One API operation, resolved and ready to become a click command."""

    path: str
    method: str
    path_params: list[Param]
    query_params: list[Param]
    body_props: list[BodyProp]
    success_response_schema: dict[str, Any] | None
    response_ref_name: str | None
    summary: str
    description: str
    group_path: list[str]
    verb: str
    # True when the request body is a raw (unmodelled) object with no named
    # properties - the whole body is accepted as one --body JSON string option.
    freeform_body: bool = False


class SpecError(RuntimeError):
    """Raised when the spec cannot be loaded or parsed."""


def load_live_spec(settings: Any | None = None) -> dict[str, Any]:
    """Return the engine OpenAPI document from the in-process app factory."""
    from dfe_engine.api.app import create_app

    app = create_app(settings=settings) if settings is not None else create_app()
    return app.openapi()


class RefResolver:
    """Minimal ``$ref`` resolver over ``#/components/schemas/...``.

    Only local component refs are resolved (the only kind FastAPI emits). A ref
    that cannot be found resolves to an empty schema rather than raising, so a
    stray pointer never takes the whole tree down.
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        self._spec = spec
        self._schemas = spec.get("components", {}).get("schemas", {})

    def ref_name(self, schema: dict[str, Any] | None) -> str | None:
        """The bare component name for a ``$ref`` schema, else None."""
        if isinstance(schema, dict) and "$ref" in schema:
            return schema["$ref"].rsplit("/", 1)[-1]
        return None

    def resolve(
        self, schema: dict[str, Any] | None, _seen: set[str] | None = None
    ) -> dict[str, Any]:
        """Resolve one ``$ref`` (non-recursively into nested props)."""
        if not isinstance(schema, dict):
            return {}
        if "$ref" not in schema:
            return schema
        name = schema["$ref"].rsplit("/", 1)[-1]
        seen = _seen or set()
        if name in seen:  # guard against self-referential schemas
            return {}
        seen.add(name)
        target = self._schemas.get(name)
        if target is None:
            return {}
        return self.resolve(target, seen)


def _scalar_type(schema: dict[str, Any]) -> str:
    """Map an (unwrapped) JSON-schema node to a CLI value type token."""
    # Optional fields come through as anyOf[type, null]; unwrap to the real type.
    if "anyOf" in schema:
        non_null = [s for s in schema["anyOf"] if s.get("type") != "null"]
        if non_null:
            return _scalar_type(non_null[0])
    t = schema.get("type")
    if t == "array":
        return "array"
    if t == "integer":
        return "int"
    if t == "number":
        return "float"
    if t == "boolean":
        return "bool"
    if t == "object" or "$ref" in schema:
        return "object"
    return "str"


def _enum_of(schema: dict[str, Any]) -> list[Any] | None:
    if "anyOf" in schema:
        for s in schema["anyOf"]:
            if s.get("enum"):
                return s["enum"]
    return schema.get("enum")


def _parse_parameters(raw_params: list[dict[str, Any]]) -> tuple[list[Param], list[Param]]:
    """Split spec parameters into (path_params, query_params)."""
    path_params: list[Param] = []
    query_params: list[Param] = []
    for p in raw_params:
        location = p.get("in")
        if location not in ("path", "query"):
            continue
        schema = p.get("schema", {})
        param = Param(
            name=p["name"],
            cli_name=_kebab(p["name"]),
            type=_scalar_type(schema),
            required=bool(p.get("required", location == "path")),
            description=p.get("description", "") or schema.get("description", ""),
            default=schema.get("default"),
            enum=_enum_of(schema),
        )
        if location == "path":
            path_params.append(param)
        else:
            query_params.append(param)
    return path_params, query_params


def _merge_all_of(
    members: list[dict[str, Any]], resolver: RefResolver
) -> tuple[dict[str, Any], list[str]]:
    """Merge the ``properties`` + ``required`` of an ``allOf`` member list.

    Each member is ref-resolved; a member may itself be an ``allOf`` (recursed).
    Later members' properties win on a name clash (rare), matching JSON-schema
    composition semantics closely enough for CLI option generation.
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    for member in members:
        resolved = resolver.resolve(member)
        member_props = resolved.get("properties")
        if not member_props and "allOf" in resolved:
            member_props, member_required = _merge_all_of(resolved["allOf"], resolver)
        else:
            member_required = resolved.get("required", [])
        if member_props:
            props.update(member_props)
        for name in member_required:
            if name not in required:
                required.append(name)
    return props, required


def _parse_body(op: dict[str, Any], resolver: RefResolver) -> tuple[list[BodyProp], bool]:
    """Flatten the JSON request body into a list of BodyProps.

    Returns ``(props, freeform)``. ``freeform`` is True for a body schema with no
    named properties (raw object) - the caller then exposes a single ``--body``
    JSON option.
    """
    body = op.get("requestBody")
    if not body:
        return [], False
    content = body.get("content", {}).get("application/json")
    if not content:
        return [], False
    schema = resolver.resolve(content.get("schema"))
    props = schema.get("properties")
    required_list = schema.get("required", [])
    if not props and "allOf" in schema:
        # A top-level ``allOf`` composition (e.g. a base model + a mixin). Merge the
        # member schemas' properties/required so each becomes an --option, rather
        # than falling back to a single opaque --body. anyOf/oneOf stay ambiguous
        # and keep the --body fallback (handled below).
        props, required_list = _merge_all_of(schema["allOf"], resolver)
    if not props:
        # Modelled as a bare object (e.g. Dict[str, Any]) - accept raw JSON.
        return [], True
    required = set(required_list)
    out: list[BodyProp] = []
    for name, prop_schema in props.items():
        out.append(
            BodyProp(
                name=name,
                cli_name=_kebab(name),
                type=_scalar_type(prop_schema),
                required=name in required,
                description=prop_schema.get("description", "") or prop_schema.get("title", ""),
                default=prop_schema.get("default"),
                enum=_enum_of(prop_schema),
            )
        )
    return out, False


def _success_response(
    op: dict[str, Any], resolver: RefResolver
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (resolved success schema, ref name) for the first 2xx response."""
    for code, resp in op.get("responses", {}).items():
        if not str(code).startswith("2"):
            continue
        content = (resp or {}).get("content", {}).get("application/json")
        if not content:
            return None, None
        raw = content.get("schema")
        return resolver.resolve(raw), resolver.ref_name(raw)
    return None, None


def iter_operations(spec: dict[str, Any]) -> list[Operation]:
    """Parse the whole spec into the flat Operation list the builder consumes."""
    resolver = RefResolver(spec)
    paths = spec.get("paths", {})

    # Collections that have a sibling item route (``.../{param}``) - used to tell a
    # REST collection (POST -> create) apart from a leaf action (POST -> <action>).
    collections_with_item: set[str] = set()
    for path in paths:
        segments = api_segments(path)
        if segments and is_param(segments[-1]):
            collections_with_item.add("/".join(segments[:-1]))

    operations: list[Operation] = []
    for path, item in paths.items():
        for method, op in item.items():
            if method.lower() not in _METHODS or not isinstance(op, dict):
                continue
            if not cli_enabled(op):
                continue
            path_params, query_params = _parse_parameters(op.get("parameters", []))
            body_props, freeform = _parse_body(op, resolver)
            success_schema, ref_name = _success_response(op, resolver)
            group_path, verb = derive_group_and_verb(path, method, collections_with_item)
            operations.append(
                Operation(
                    path=path,
                    method=method.lower(),
                    path_params=path_params,
                    query_params=query_params,
                    body_props=body_props,
                    success_response_schema=success_schema,
                    response_ref_name=ref_name,
                    summary=op.get("summary", "") or "",
                    description=op.get("description", "") or "",
                    group_path=group_path,
                    verb=verb,
                    freeform_body=freeform,
                )
            )
    return operations
