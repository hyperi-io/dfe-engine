#  Project:      dfe-engine
#  File:         src/dfe_engine/services/schema/elastic_schema_service.py
#  Purpose:      Convert Elasticsearch index template mappings to meta-schema columns
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Elastic index template → DFE meta-schema ``SchemaColumn`` definitions.

Converts Elasticsearch index template mappings to DFE meta-schema ``SchemaColumn`` definitions.

Walks ``mappings.properties`` (Beat-style ``template.mappings`` or top-level
``mappings``), emits ``SchemaColumn`` rows aligned with YAML conventions such as
snake_case ``name``, ``@source:`` dotted ``expr``, primitive ``type``,
and heuristic ``use_case`` / ``attribute``.
"""

from __future__ import annotations

import json
from typing import Any

from dfe_engine.schema.models import SchemaColumn

ELASTIC_IMPORT_FIELD_TYPE = "elastic_imported"


class ElasticSchemaConversionError(ValueError):
    """Raised when JSON is not a usable Elastic template / mappings document."""


def _join_path(prefix: str, segment: str) -> str:
    return f"{prefix}.{segment}" if prefix else segment


def _column_name_from_field_path(field_path: str) -> str:
    """Build a ClickHouse-friendly column name; keeps dotted paths as underscore segments."""

    parts = field_path.split(".")
    cleaned: list[str] = []
    for segment in parts:
        if segment.startswith("@"):
            segment = segment[1:]
        cleaned.append(segment)
    return "_".join(cleaned)


def _map_es_type(
    es_type: str,
) -> tuple[str, list[str], str]:
    """Map Elasticsearch field type to DFE primitive, attributes, and use_case."""

    match es_type:
        case "keyword" | "wildcard" | "constant_keyword" | "version":
            return "string", ["lowcardinality"], "dimension"
        case "text" | "match_only_text":
            return "text", [], "fulltext"
        case "long" | "integer" | "short" | "byte" | "unsigned_long":
            return "integer", [], "range"
        case "double" | "float" | "half_float" | "scaled_float":
            return "float", [], "range"
        case "boolean":
            return "boolean", [], "dimension"
        case "date" | "date_nanos":
            return "datetime", [], "range"
        case "ip":
            return "ip", [], "dimension"
        case "geo_point":
            return "geo_point", [], ""
        case "geo_shape":
            return "json", ["nullable"], ""
        case "flattened" | "nested":
            return "json", ["nullable"], ""
        case "object":
            return "json", ["nullable"], ""
        case "binary" | "rank_feature" | "rank_features":
            return "json", ["nullable"], ""
        case _:
            return "json", ["nullable"], ""


def _walk_mapping(prefix: str, mapping: dict[str, Any], columns: list[SchemaColumn]) -> None:
    if not isinstance(mapping, dict):
        return

    es_type = mapping.get("type")
    props = mapping.get("properties")
    props_dict = props if isinstance(props, dict) else None

    # Object / nested containers with explicit child mappings
    if props_dict is not None and es_type in (None, "object", "nested"):
        for key, sub in props_dict.items():
            _walk_mapping(_join_path(prefix, key), sub, columns)
        return

    if es_type == "alias":
        return

    # Implicit object with no explicit type string (some exports omit type: object)
    if props_dict is not None and not isinstance(es_type, str):
        for key, sub in props_dict.items():
            _walk_mapping(_join_path(prefix, key), sub, columns)
        return

    # Empty object — store as JSON blob at this path
    if es_type == "object" and props_dict is None:
        primitive, attrs, use_case = _map_es_type("object")
        fp = prefix
        columns.append(
            SchemaColumn(
                name=_column_name_from_field_path(fp),
                type=primitive,
                attribute=list(attrs),
                use_case=use_case,
                expr=f"@source: {fp}",
                comment="Elasticsearch mapping type: object",
                field_type=ELASTIC_IMPORT_FIELD_TYPE,
            )
        )
        return

    # Malformed leaf that still declares properties — descend to salvage
    if props_dict is not None:
        for key, sub in props_dict.items():
            _walk_mapping(_join_path(prefix, key), sub, columns)
        return

    if isinstance(es_type, str) and es_type not in ("object", "nested"):
        primitive, attrs, use_case = _map_es_type(es_type)
        fp = prefix
        comment = f"Elasticsearch mapping type: {es_type}"
        columns.append(
            SchemaColumn(
                name=_column_name_from_field_path(fp),
                type=primitive,
                attribute=list(attrs),
                use_case=use_case,
                expr=f"@source: {fp}",
                comment=comment,
                field_type=ELASTIC_IMPORT_FIELD_TYPE,
            )
        )

    inner_fields = mapping.get("fields")
    if isinstance(inner_fields, dict):
        for fk, fv in inner_fields.items():
            _walk_mapping(_join_path(prefix, fk), fv, columns)


def _extract_mappings_properties(doc: dict[str, Any]) -> dict[str, Any]:
    """Resolve mappings.properties from common Elastic export shapes."""

    if not isinstance(doc, dict):
        raise ElasticSchemaConversionError("Document root must be a JSON object")

    mappings: dict[str, Any] | None = None

    template = doc.get("template")
    if isinstance(template, dict):
        inner = template.get("mappings")
        if isinstance(inner, dict):
            mappings = inner

    if mappings is None:
        root_map = doc.get("mappings")
        if isinstance(root_map, dict):
            mappings = root_map

    if mappings is None:
        raise ElasticSchemaConversionError(
            "Expected 'template.mappings', top-level 'mappings', or compatible shape"
        )

    props = mappings.get("properties")
    if not isinstance(props, dict):
        raise ElasticSchemaConversionError("mappings.properties is missing or not an object")

    return props


class ElasticSchemaService:
    """Convert Elasticsearch index template JSON to meta-schema columns."""

    @staticmethod
    def template_dict_to_columns(doc: dict[str, Any]) -> list[SchemaColumn]:
        props = _extract_mappings_properties(doc)
        columns: list[SchemaColumn] = []
        for field_name, field_mapping in props.items():
            _walk_mapping(field_name, field_mapping, columns)
        columns.sort(key=lambda c: c.name)
        return columns

    @staticmethod
    def template_json_to_columns(raw: bytes | str | dict[str, Any]) -> list[SchemaColumn]:
        if isinstance(raw, dict):
            return ElasticSchemaService.template_dict_to_columns(raw)
        if isinstance(raw, bytes):
            text = raw.decode("utf-8")
        else:
            text = raw
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ElasticSchemaConversionError(f"Invalid JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise ElasticSchemaConversionError("JSON root must be an object")
        return ElasticSchemaService.template_dict_to_columns(doc)
