#  Project:      dfe-engine
#  File:         src/dfe_engine/services/schema/elastic_schema_service.py
#  Purpose:      Convert Elasticsearch index template mappings to meta-schema columns
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Elastic index template -> DFE meta-schema ``SchemaColumn`` definitions.

Walks ``mappings.properties`` (Beat-style ``template.mappings`` or top-level
``mappings``), emits ``SchemaColumn`` rows aligned with YAML conventions such as
snake_case ``name``, ``@source:`` dotted ``expr``, primitive ``type``,
and the ``use_case`` / ``attribute`` the template declares.

CARDINALITY IS NOT IN A TEMPLATE. An index template says a field is a ``keyword``;
it never says whether that keyword holds three values or three million. So the
importer sets a ``use_case`` only where the template states the intent - ``text``
means word search, ``date`` and the numerics mean ranges, ``boolean`` and
``constant_keyword`` are bounded by definition - and leaves a plain ``keyword``
with no ``use_case`` and no ``lowcardinality`` for the field picker to decide.
Guessing them produced ``LowCardinality(String)`` plus a ``set(0)`` index on
``url.full`` and ``event.original``, which costs more than it saves, and a
column that already looks decided never gets re-reviewed. Those columns carry
``cardinality not measured`` in the comment, so a reader can tell an unanswered
question from a column judged to need no index. Measuring it belongs where the
data is - promote time and derived selection - not here.

PHYSICAL-ONLY. The importer emits the source's PHYSICAL columns (the raw ES field
path -> snake_case name, kept as ``@source:``); it does NOT bake in a naming
standard. ECS / Sigma / CIM naming is a read-time REMAP VIEW
(``fieldmap.remap_view``, ``{source}_ecs`` etc.), so a beats source's ECS column
names come from the ECS view over these physical columns, not from the importer.

SUBTREE SELECTION. ``template_*_to_columns(..., roots=[...])`` imports only the
named top-level subtrees (JSON root scalars + common + ECS + the module subtree
like ``aws``) - the mechanism a per-MODULE beats source uses to import just its
slice of the monolithic filebeat template.

SIMPLE PRIMITIVES FIRST (by design, a PRIMARY meta-schema benefit). The importer maps
to the meta-schema's SIMPLE primitives (``integer``, ``datetime``, ``float``), NOT the
exact ClickHouse type (Int8 / UInt64 / DateTime64(9)). That simplification IS the point
- a clean, portable starting schema. A user who wants to min-max to an exact CH type
does so AFTER, per column, via a ``ch_override`` (e.g. ES ``unsigned_long`` -> ``UInt64``,
``date_nanos`` -> ``DateTime64(9)``). So ``unsigned_long`` -> ``integer`` and
``date_nanos`` -> ``datetime`` is correct-BY-DEFAULT, not a gap.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from dfe_engine.schema.models import SchemaColumn

ELASTIC_IMPORT_FIELD_TYPE = "elastic_imported"

# Marks a column whose index choice needs a cardinality the template cannot supply,
# so a reader can tell it apart from one judged not to need an index.
CARDINALITY_NOT_MEASURED = "cardinality not measured"


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


def _index_suppressed(mapping: dict[str, Any]) -> bool:
    """Elastic's own declaration that a field is not searchable or not aggregatable."""

    return mapping.get("index") is False or mapping.get("doc_values") is False


def _map_es_type(
    es_type: str,
) -> tuple[str, list[str], str]:
    """Map Elasticsearch field type to DFE primitive, attributes, and use_case.

    Only the use cases the template actually DECLARES are set. A plain ``keyword``
    covers both a three-value enum and an unbounded URL, so it gets neither
    ``lowcardinality`` nor a ``use_case`` - the picker decides those.
    """

    match es_type:
        case "keyword" | "wildcard" | "version":
            return "string", [], ""
        case "constant_keyword":
            return "string", ["lowcardinality"], "dimension"
        case "text" | "match_only_text":
            return "text", [], "word_search"
        case "long" | "integer" | "short" | "byte" | "unsigned_long":
            return "integer", [], "range"
        case "double" | "float" | "half_float" | "scaled_float":
            return "float", [], "range"
        case "boolean":
            return "boolean", [], "dimension"
        case "date" | "date_nanos":
            return "datetime", [], "range"
        case "ip":
            return "ip", [], "range"
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
        if _index_suppressed(mapping):
            # `index: false` / `doc_values: false` is the template declaring the
            # field is not searched or not grouped by - it beats the type default.
            attrs = [attr for attr in attrs if attr != "lowcardinality"]
            use_case = ""
        elif primitive == "string" and not use_case:
            comment = f"{comment} ({CARDINALITY_NOT_MEASURED})"
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


def _fields_to_properties(entries: Any) -> dict[str, Any]:
    """Convert a beats ``fields.yml`` field list into the ``mappings.properties`` shape
    the ES-template importer already walks - so a beats schema re-uses the SAME
    column-mapping code path (no second importer).

    A ``group`` (or an entry with nested ``fields`` and no leaf ``type``) becomes a
    ``properties`` container; an ``alias`` keeps ``type: alias`` (skipped downstream,
    exactly as in an ES template); a leaf keeps its ES ``type``. A dotted ``name``
    (e.g. ``body_sent.bytes``) passes through as the property key and splits into the
    column path downstream, so both the nested-group and flat-dotted beats shapes work.
    """
    props: dict[str, Any] = {}
    if not isinstance(entries, list):
        return props
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # A `key`-only entry is beats MODULE metadata (key/title/description + fields),
        # NOT a field - flatten its fields at this level (the module namespace is the
        # inner `name: <module>` group, not the `key` itself).
        if entry.get("key") and not entry.get("name"):
            props.update(_fields_to_properties(entry.get("fields") or []))
            continue
        name = entry.get("name")
        if not name:
            continue
        etype = entry.get("type")
        sub = entry.get("fields")
        if etype == "group" or (sub is not None and etype is None):
            props[str(name)] = {"properties": _fields_to_properties(sub or [])}
        elif etype == "alias":
            props[str(name)] = {"type": "alias"}
        elif sub is not None:
            props[str(name)] = {"properties": _fields_to_properties(sub)}
        else:
            props[str(name)] = {"type": etype or "keyword"}
    return props


class ElasticSchemaService:
    """Convert Elasticsearch index template JSON to meta-schema columns."""

    @staticmethod
    def template_dict_to_columns(
        doc: dict[str, Any], roots: Iterable[str] | None = None
    ) -> list[SchemaColumn]:
        """Convert an Elastic template dict to physical columns.

        ``roots`` (subtree selection): when given, only the named top-level
        ``mappings.properties`` entries are imported - the JSON root scalars you
        want (``@timestamp``, ``message``, ``tags``) PLUS the subtrees you want
        (``host``, ``agent``, ``event``, ``ecs``, and the module-specific like
        ``aws``). This is how a per-MODULE beats source (``filebeat_aws``) imports
        just common + ECS + its own subtree instead of the whole monolithic
        filebeat template. ``None`` imports everything (back-compat).
        """
        props = _extract_mappings_properties(doc)
        wanted = set(roots) if roots is not None else None
        columns: list[SchemaColumn] = []
        for field_name, field_mapping in props.items():
            if wanted is not None and field_name not in wanted:
                continue
            _walk_mapping(field_name, field_mapping, columns)
        columns.sort(key=lambda c: c.name)
        return columns

    @staticmethod
    def template_json_to_columns(
        raw: bytes | str | dict[str, Any], roots: Iterable[str] | None = None
    ) -> list[SchemaColumn]:
        if isinstance(raw, dict):
            return ElasticSchemaService.template_dict_to_columns(raw, roots)
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
        return ElasticSchemaService.template_dict_to_columns(doc, roots)

    @staticmethod
    def beats_fields_to_columns(
        fields: Any, roots: Iterable[str] | None = None
    ) -> list[SchemaColumn]:
        """Convert a beats ``fields.yml`` (parsed YAML - a list of field groups) to
        physical columns, RE-USING the ES-template importer.

        Same `_map_es_type` + physical naming + subtree-selection as an index-template
        import - just fed from the beats field format instead of a generated template.
        Re-running it over a module's ``fields.yml`` refreshes a source's columns so
        incoming data keeps landing as the format evolves. filebeat is imported
        PER-MODULE (one call per module `fields.yml`); other beats whole.
        """
        props = _fields_to_properties(fields)
        if not props:
            raise ElasticSchemaConversionError(
                "fields.yml produced no properties (expected a list of field groups)"
            )
        return ElasticSchemaService.template_dict_to_columns(
            {"mappings": {"properties": props}}, roots
        )
