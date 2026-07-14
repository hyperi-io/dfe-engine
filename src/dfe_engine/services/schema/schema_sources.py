#  Project:      dfe-engine
#  File:         services/schema/schema_sources.py
#  Purpose:      Pluggable framework: external schema definition -> DFE physical columns
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A standard, extensible framework for auto-aligning DFE pre-supplied schemas to an
external schema source.

ONE common core (the type-mapping + physical naming in :mod:`elastic_schema_service`);
per-FORMAT ADAPTERS feed it. Adding a new external source - e.g. a CrowdStrike feed
schema in JSON or YAML - is ONE new adapter registered here; nothing else changes.

Formats shipped now:
- ``elastic_template`` - an Elasticsearch index template (Beat-style or API-style JSON).
- ``beats_fields`` - a beats ``fields.yml`` (the module/fileset field SSoT).

Both re-use the SAME importer (physical-only columns, ``@source:`` paths, subtree
selection), so a source's DFE schema is refreshed by re-running its adapter over the
schema definition.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from dfe_engine.schema.models import SchemaColumn
from dfe_engine.services.schema.elastic_schema_service import (
    ElasticSchemaConversionError,
    ElasticSchemaService,
)
from dfe_engine.yaml_utils import yaml_load_string

# An adapter turns a raw external schema definition (a parsed object, or text/bytes to
# parse) plus an optional subtree-selection into DFE physical columns.
SchemaAdapter = Callable[[Any, Iterable[str] | None], list[SchemaColumn]]


def _elastic_template_adapter(raw: Any, roots: Iterable[str] | None = None) -> list[SchemaColumn]:
    """Elasticsearch index template (bytes/str JSON, or a parsed dict)."""
    return ElasticSchemaService.template_json_to_columns(raw, roots)


def _beats_fields_adapter(raw: Any, roots: Iterable[str] | None = None) -> list[SchemaColumn]:
    """A beats ``fields.yml`` (YAML text/bytes, or an already-parsed list)."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = yaml_load_string(raw)
    return ElasticSchemaService.beats_fields_to_columns(raw, roots)


# JSON Schema string ``format`` -> the ES type the importer already maps to a primitive.
_JSON_SCHEMA_FORMAT_TO_ES = {
    "date-time": "date",
    "date": "date",
    "time": "date",
    "ipv4": "ip",
    "ipv6": "ip",
    "uuid": "keyword",
    "email": "keyword",
    "hostname": "keyword",
    "uri": "keyword",
    "idn-hostname": "keyword",
}
_JSON_SCHEMA_TYPE_TO_ES = {
    "string": "keyword",
    "integer": "long",
    "number": "double",
    "boolean": "boolean",
}


def _json_schema_es_type(prop: dict[str, Any]) -> str:
    fmt = prop.get("format")
    if isinstance(fmt, str) and fmt in _JSON_SCHEMA_FORMAT_TO_ES:
        return _JSON_SCHEMA_FORMAT_TO_ES[fmt]
    t = prop.get("type")
    if isinstance(t, list):  # e.g. ["string", "null"] - take the non-null
        t = next((x for x in t if x != "null"), "string")
    return _JSON_SCHEMA_TYPE_TO_ES.get(t, "keyword")


def _json_schema_to_properties(schema: Any) -> dict[str, Any]:
    """JSON Schema (draft) object -> the ES ``mappings.properties`` shape, so a JSON
    Schema re-uses the SAME importer. Nested ``object`` recurses; ``array`` and a typeless
    object collapse to a JSON blob (as ES flattened/nested do)."""
    props: dict[str, Any] = {}
    schema_props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(schema_props, dict):
        return props
    for name, prop in schema_props.items():
        if not isinstance(prop, dict):
            continue
        t = prop.get("type")
        if t == "object" and isinstance(prop.get("properties"), dict):
            props[str(name)] = {"properties": _json_schema_to_properties(prop)}
        elif t == "array" or (t == "object" and "properties" not in prop):
            props[str(name)] = {"type": "object"}  # -> json blob
        else:
            props[str(name)] = {"type": _json_schema_es_type(prop)}
    return props


def _json_schema_adapter(raw: Any, roots: Iterable[str] | None = None) -> list[SchemaColumn]:
    """A JSON Schema (draft 2020-12 etc.) as JSON/YAML text or a parsed dict. Also the
    substrate for OCSF, which is delivered as JSON."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = json.loads(raw)
    props = _json_schema_to_properties(raw)
    if not props:
        raise ElasticSchemaConversionError(
            "JSON Schema produced no properties (expected an object schema with 'properties')"
        )
    return ElasticSchemaService.template_dict_to_columns({"mappings": {"properties": props}}, roots)


# OCSF data-type (the `_t` types) -> the ES type the importer maps to a primitive.
_OCSF_TYPE_TO_ES = {
    "string_t": "keyword",
    "integer_t": "long",
    "long_t": "long",
    "float_t": "double",
    "boolean_t": "boolean",
    "datetime_t": "date",
    "timestamp_t": "date",
    "ip_t": "ip",
    "port_t": "integer",
    "json_t": "object",
    "object_t": "object",  # nested OCSF object (referential) -> json blob for now
}


def _ocsf_attrs(schema: Any):
    """Yield (name, definition) for an OCSF class' attributes, whether the API's
    list-of-single-key-dicts form or the repo's `{name: def}` dict form."""
    attrs = schema.get("attributes") if isinstance(schema, dict) else None
    if isinstance(attrs, dict):
        yield from attrs.items()
    elif isinstance(attrs, list):
        for item in attrs:
            if isinstance(item, dict):
                yield from item.items()


def _ocsf_to_properties(schema: Any) -> dict[str, Any]:
    """A resolved OCSF class/object -> the ES `mappings.properties` shape (reuse the
    importer). `is_array` and nested objects collapse to a JSON blob."""
    props: dict[str, Any] = {}
    for name, defn in _ocsf_attrs(schema):
        if not isinstance(defn, dict):
            continue
        if defn.get("is_array"):
            props[str(name)] = {"type": "object"}
        else:
            props[str(name)] = {"type": _OCSF_TYPE_TO_ES.get(defn.get("type"), "keyword")}
    return props


def _ocsf_adapter(raw: Any, roots: Iterable[str] | None = None) -> list[SchemaColumn]:
    """A resolved OCSF event class / object (JSON text or parsed dict).

    OCSF is the leading security-event schema (AWS Security Lake canonical) and is
    delivered as JSON, so it rides the JSON substrate; only the OCSF `_t` type map
    differs from a plain JSON Schema.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = json.loads(raw)
    props = _ocsf_to_properties(raw)
    if not props:
        raise ElasticSchemaConversionError(
            "OCSF schema produced no properties (expected a class/object with 'attributes')"
        )
    return ElasticSchemaService.template_dict_to_columns({"mappings": {"properties": props}}, roots)


# The framework registry: format name -> adapter. EXTEND by adding an entry.
ADAPTERS: dict[str, SchemaAdapter] = {
    "elastic_template": _elastic_template_adapter,
    "beats_fields": _beats_fields_adapter,
    "json_schema": _json_schema_adapter,
    "ocsf": _ocsf_adapter,
}


def known_formats() -> list[str]:
    """The registered schema-source formats."""
    return sorted(ADAPTERS)


def register_adapter(fmt: str, adapter: SchemaAdapter) -> None:
    """Register a new external-schema format (e.g. a CrowdStrike feed schema)."""
    ADAPTERS[fmt] = adapter


def import_schema(fmt: str, raw: Any, roots: Iterable[str] | None = None) -> list[SchemaColumn]:
    """Import an external schema definition to DFE physical columns via its adapter.

    Args:
        fmt: the registered format (see :func:`known_formats`).
        raw: the schema definition - a parsed object, or text/bytes to parse.
        roots: optional subtree-selection (the per-module beats import).
    """
    adapter = ADAPTERS.get(fmt)
    if adapter is None:
        raise ElasticSchemaConversionError(
            f"unknown schema-source format {fmt!r}; known: {known_formats()}"
        )
    return adapter(raw, roots)
