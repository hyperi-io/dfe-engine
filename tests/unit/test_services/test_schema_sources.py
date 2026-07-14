#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_schema_sources.py
#  Purpose:      Pluggable schema-source framework (external def -> DFE columns)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The standard schema-source framework: one common importer, per-format adapters.

Includes a check that the importer reads a beats-format `fields.yml` file, plus
type-variety coverage and the extensibility seam (register a new format = one adapter).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.services.schema.elastic_schema_service import ElasticSchemaConversionError
from dfe_engine.services.schema.schema_sources import (
    ADAPTERS,
    import_schema,
    known_formats,
    register_adapter,
)

_RESOURCES = Path(__file__).resolve().parents[2] / "resources" / "schema_sources"


# -- File-based check: the adapter reads a beats-format fields.yml file --------------


def test_beats_fields_adapter_reads_a_fields_yml_file() -> None:
    """A beats-format fields.yml file imports cleanly through the adapter: a real leaf
    becomes a physical column, `type: alias` entries are skipped, groups flatten."""
    raw = (_RESOURCES / "sample_service_fields.yml").read_text()
    cols = import_schema("beats_fields", raw)
    names = {c.name for c in cols}
    assert "sample_client_addr" in names
    assert "sample_sent_bytes" in names
    # alias entries (which point at a naming standard) are NOT physical columns
    assert "sample_verb" not in names
    assert "sample_status" not in names
    # everything carries the physical @source: path (physical-only importer)
    assert all(c.expr and c.expr.startswith("@source: ") for c in cols)


# -- Type variety through the SAME _map_es_type core ------------------------------


RICH_BEATS_FIELDS = [
    {
        "key": "demo",
        "fields": [
            {
                "name": "demo",
                "type": "group",
                "fields": [
                    {"name": "client_ip", "type": "ip"},
                    {"name": "bytes", "type": "long"},
                    {"name": "created", "type": "date"},
                    {"name": "tag", "type": "keyword"},
                    {"name": "legacy_user", "type": "alias", "path": "user.name"},
                    {
                        "name": "nested",
                        "type": "group",
                        "fields": [{"name": "value", "type": "keyword"}],
                    },
                ],
            }
        ],
    }
]


def test_beats_fields_type_mapping_and_nesting() -> None:
    cols = {c.name: c for c in import_schema("beats_fields", RICH_BEATS_FIELDS)}
    assert cols["demo_client_ip"].type == "ip"
    assert cols["demo_bytes"].type == "integer"
    assert cols["demo_created"].type == "datetime"
    assert cols["demo_tag"].type == "string"
    assert cols["demo_nested_value"].type == "string"  # nested group flattened
    assert "demo_legacy_user" not in cols  # alias skipped, like an ES template


def test_beats_fields_subtree_selection() -> None:
    """roots reaches the same subtree-selection the ES-template path uses."""
    cols = {c.name for c in import_schema("beats_fields", RICH_BEATS_FIELDS, roots=["demo"])}
    assert "demo_client_ip" in cols
    empty = import_schema("beats_fields", RICH_BEATS_FIELDS, roots=["nonexistent"])
    assert empty == []


# -- The elastic_template adapter goes through the same framework ------------------


def test_elastic_template_adapter_via_framework() -> None:
    doc = {"mappings": {"properties": {"pid": {"type": "long"}, "src": {"type": "ip"}}}}
    cols = {c.name: c.type for c in import_schema("elastic_template", doc)}
    assert cols == {"pid": "integer", "src": "ip"}


def test_json_schema_adapter_via_framework() -> None:
    """A JSON Schema (the general 80% standard; OCSF is delivered as JSON) runs through
    the same importer - format-aware (ipv4/date-time), nesting, arrays -> json blob."""
    schema = {
        "type": "object",
        "properties": {
            "src_ip": {"type": "string", "format": "ipv4"},
            "when": {"type": "string", "format": "date-time"},
            "count": {"type": "integer"},
            "ratio": {"type": "number"},
            "ok": {"type": "boolean"},
            "name": {"type": ["string", "null"]},
            "nested": {"type": "object", "properties": {"leaf": {"type": "string"}}},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
    }
    cols = {c.name: c.type for c in import_schema("json_schema", schema)}
    assert cols["src_ip"] == "ip"
    assert cols["when"] == "datetime"
    assert cols["count"] == "integer"
    assert cols["ratio"] == "float"
    assert cols["ok"] == "boolean"
    assert cols["name"] == "string"
    assert cols["nested_leaf"] == "string"
    assert cols["tags"] == "json"


def test_ocsf_adapter_class_and_dict_forms() -> None:
    """OCSF (the security-event standard, AWS Security Lake canonical) - both the API's
    list-of-single-key-dicts and the repo's dict form, OCSF `_t` types mapped."""
    # API form: attributes = list of {name: {type, ...}}
    api_form = {
        "name": "authentication",
        "attributes": [
            {"src_ip": {"type": "ip_t", "type_name": "IP Address"}},
            {"when": {"type": "datetime_t"}},
            {"severity_id": {"type": "integer_t"}},
            {"activity_name": {"type": "string_t"}},
            {"is_cleartext": {"type": "boolean_t"}},
            {"tags": {"type": "string_t", "is_array": True}},
            {"metadata": {"type": "object_t", "object_type": "metadata"}},
        ],
    }
    cols = {c.name: c.type for c in import_schema("ocsf", api_form)}
    assert cols["src_ip"] == "ip"
    assert cols["when"] == "datetime"
    assert cols["severity_id"] == "integer"
    assert cols["activity_name"] == "string"
    assert cols["is_cleartext"] == "boolean"
    assert cols["tags"] == "json"  # is_array -> blob
    assert cols["metadata"] == "json"  # nested OCSF object -> blob

    # repo form: attributes = {name: {type,...}} - same result
    repo_form = {"attributes": {"port": {"type": "port_t"}, "user": {"type": "string_t"}}}
    cols2 = {c.name: c.type for c in import_schema("ocsf", repo_form)}
    assert cols2 == {"port": "integer", "user": "string"}


# -- Framework: registry + dispatch + extensibility -------------------------------


def test_known_formats_ships_all() -> None:
    assert {"elastic_template", "beats_fields", "json_schema", "ocsf"} <= set(known_formats())


def test_unknown_format_raises() -> None:
    with pytest.raises(ElasticSchemaConversionError, match="unknown schema-source format"):
        import_schema("crowdstrike_json", {})


def test_register_new_adapter_extends_framework() -> None:
    """Adding a source (e.g. a CrowdStrike feed schema) is ONE adapter, nothing else."""
    sentinel = object()
    try:
        register_adapter("crowdstrike_json", lambda raw, roots=None: [])
        assert "crowdstrike_json" in known_formats()
        assert import_schema("crowdstrike_json", sentinel) == []
    finally:
        ADAPTERS.pop("crowdstrike_json", None)
