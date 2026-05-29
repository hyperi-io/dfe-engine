#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_elastic_schema_service.py
#  Purpose:      Tests for Elasticsearch template → meta-schema conversion
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json

import pytest

from dfe_engine.services.schema.elastic_schema_service import (
    ElasticSchemaConversionError,
    ElasticSchemaService,
)

MINIMAL_BEAT_TEMPLATE = {
    "template": {
        "mappings": {
            "properties": {
                "container": {
                    "properties": {
                        "name": {"ignore_above": 1024, "type": "keyword"},
                        "labels": {"type": "object"},
                    }
                },
                "@timestamp": {"type": "date"},
                "message": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}},
                },
                "event": {
                    "properties": {"dataset": {"type": "keyword"}},
                },
            }
        }
    }
}


def test_template_dict_to_columns_keyword_nested_and_timestamp_name() -> None:
    cols = ElasticSchemaService.template_dict_to_columns(MINIMAL_BEAT_TEMPLATE)
    by_name = {c.name: c for c in cols}

    assert by_name["container_name"].type == "string"
    assert by_name["container_name"].expr == "@source: container.name"
    assert "lowcardinality" in by_name["container_name"].attribute

    assert by_name["timestamp"].expr == "@source: @timestamp"

    assert by_name["message"].type == "text"
    assert by_name["message_keyword"].expr == "@source: message.keyword"

    assert by_name["event_dataset"].type == "string"


def test_empty_object_emits_json_column() -> None:
    cols = ElasticSchemaService.template_dict_to_columns(MINIMAL_BEAT_TEMPLATE)
    labels = next(c for c in cols if c.name == "container_labels")
    assert labels.type == "json"
    assert labels.expr == "@source: container.labels"


def test_template_json_to_columns_from_json_string() -> None:
    raw = json.dumps(MINIMAL_BEAT_TEMPLATE)
    cols = ElasticSchemaService.template_json_to_columns(raw)
    assert len(cols) >= 5
    names = {c.name for c in cols}
    assert "container_name" in names
    assert "timestamp" in names


def test_invalid_json_raises() -> None:
    with pytest.raises(ElasticSchemaConversionError, match="Invalid JSON"):
        ElasticSchemaService.template_json_to_columns("not json {{{")


def test_missing_mappings_raises() -> None:
    with pytest.raises(ElasticSchemaConversionError, match="Expected"):
        ElasticSchemaService.template_dict_to_columns({"foo": 1})


def test_top_level_mappings_shape() -> None:
    doc = {"mappings": {"properties": {"pid": {"type": "long"}}}}
    cols = ElasticSchemaService.template_dict_to_columns(doc)
    by_name = {c.name: c for c in cols}
    assert by_name["pid"].type == "integer"


def test_mappings_properties_missing_raises() -> None:
    with pytest.raises(ElasticSchemaConversionError, match=r"mappings\.properties"):
        ElasticSchemaService.template_dict_to_columns({"mappings": {}})
    with pytest.raises(ElasticSchemaConversionError, match=r"mappings\.properties"):
        ElasticSchemaService.template_dict_to_columns({"mappings": {"properties": []}})


def test_document_root_not_object_raises() -> None:
    with pytest.raises(ElasticSchemaConversionError, match="Document root"):
        ElasticSchemaService.template_dict_to_columns([])  # type: ignore[arg-type]


def test_template_json_dict_and_bytes_paths() -> None:
    cols_dict = ElasticSchemaService.template_json_to_columns(MINIMAL_BEAT_TEMPLATE)
    raw = json.dumps(MINIMAL_BEAT_TEMPLATE).encode("utf-8")
    cols_bytes = ElasticSchemaService.template_json_to_columns(raw)
    assert {c.name for c in cols_dict} == {c.name for c in cols_bytes}


def test_template_json_non_object_root_raises() -> None:
    with pytest.raises(ElasticSchemaConversionError, match="JSON root must be an object"):
        ElasticSchemaService.template_json_to_columns("[1]")


def test_es_type_mapping_variants() -> None:
    doc = {
        "mappings": {
            "properties": {
                "wc": {"type": "wildcard"},
                "ck": {"type": "constant_keyword"},
                "ver": {"type": "version"},
                "mot": {"type": "match_only_text"},
                "sh": {"type": "short"},
                "by": {"type": "byte"},
                "ul": {"type": "unsigned_long"},
                "hf": {"type": "half_float"},
                "sf": {"type": "scaled_float"},
                "dn": {"type": "date_nanos"},
                "ipf": {"type": "ip"},
                "gp": {"type": "geo_point"},
                "gs": {"type": "geo_shape"},
                "flat": {"type": "flattened"},
                "bin": {"type": "binary"},
                "rf": {"type": "rank_feature"},
                "rfs": {"type": "rank_features"},
                "weird": {"type": "unknown_es_type"},
                "boolf": {"type": "boolean"},
                "dbl": {"type": "double"},
            }
        }
    }
    cols = ElasticSchemaService.template_dict_to_columns(doc)
    by_name = {c.name: c for c in cols}

    assert by_name["wc"].type == "string"
    assert by_name["mot"].type == "text"
    assert by_name["sh"].type == "integer"
    assert by_name["hf"].type == "float"
    assert by_name["boolf"].type == "boolean"
    assert by_name["ipf"].type == "ip"
    assert by_name["gp"].type == "geo_point"
    for json_name in ("gs", "flat", "bin", "rf", "rfs", "weird"):
        assert by_name[json_name].type == "json"
    assert by_name["dn"].type == "datetime"


def test_alias_field_skipped() -> None:
    doc = {
        "mappings": {
            "properties": {
                "real": {"type": "keyword"},
                "alias_field": {"type": "alias", "path": "real"},
            }
        }
    }
    cols = ElasticSchemaService.template_dict_to_columns(doc)
    assert {c.name for c in cols} == {"real"}


def test_non_dict_property_value_skipped() -> None:
    doc = {"mappings": {"properties": {"broken": "not-a-mapping"}}}
    cols = ElasticSchemaService.template_dict_to_columns(doc)
    assert cols == []


def test_implicit_object_numeric_type_descends() -> None:
    doc = {
        "mappings": {
            "properties": {
                "outer": {"type": 1, "properties": {"inner": {"type": "keyword"}}}  # type: ignore[dict-item]
            }
        }
    }
    cols = ElasticSchemaService.template_dict_to_columns(doc)
    by_name = {c.name: c for c in cols}
    assert by_name["outer_inner"].type == "string"


def test_malformed_leaf_with_properties_descends() -> None:
    doc = {
        "mappings": {
            "properties": {
                "odd": {
                    "type": "keyword",
                    "properties": {"nested_kw": {"type": "keyword"}},
                }
            }
        }
    }
    cols = ElasticSchemaService.template_dict_to_columns(doc)
    by_name = {c.name: c for c in cols}
    # Keyword + nested properties is treated as container-only; children are emitted.
    assert "odd" not in by_name
    assert by_name["odd_nested_kw"].type == "string"


def test_schema_subpackage_reexports() -> None:
    import dfe_engine.services.schema as schema_pkg

    assert schema_pkg.ElasticSchemaService is ElasticSchemaService
    assert schema_pkg.ElasticSchemaConversionError is ElasticSchemaConversionError
