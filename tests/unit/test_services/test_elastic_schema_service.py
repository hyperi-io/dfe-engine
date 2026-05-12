#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_elastic_schema_service.py
#  Purpose:      Tests for Elasticsearch template → meta-schema conversion
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
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
