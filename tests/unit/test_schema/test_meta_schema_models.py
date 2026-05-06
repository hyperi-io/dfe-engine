"""Tests for meta-schema API models (SchemaSummary tree + MetaSchema)."""

from __future__ import annotations

import pydantic
import pytest

from dfe_engine.schema.models import (
    MetaSchema,
    PaginatedSchemaSummaryResponse,
    SchemaSummary,
    SchemaSummaryObject,
)


@pytest.fixture
def cloudtrail_like_yaml() -> dict:
    return {
        "current": "1.0.0",
        "description": "AWS CloudTrail",
        "versions": {
            "1.0.0": {
                "date": "2026-03-03",
                "type": "model",
                "summary": "Initial",
                "columns": [
                    {
                        "name": "event_id",
                        "type": "string",
                        "expr": "@source: EventId",
                        "comment": "id",
                    }
                ],
            }
        },
    }


class TestMetaSchema:
    def test_validate_shipped_shape(self, cloudtrail_like_yaml: dict):
        ms = MetaSchema.model_validate(cloudtrail_like_yaml)
        assert ms.current == "1.0.0"
        assert ms.description == "AWS CloudTrail"
        assert len(ms.versions["1.0.0"].columns) == 1
        assert ms.versions["1.0.0"].columns[0].name == "event_id"

    def test_to_yaml_dict_omits_path(self, cloudtrail_like_yaml: dict):
        ms = MetaSchema.model_validate(
            {**cloudtrail_like_yaml, "path": "aws/cloudtrail"},
        )
        dumped = ms.to_yaml_dict()
        assert "path" not in dumped
        assert dumped["current"] == "1.0.0"


class TestSchemaSummaryTree:
    def test_coerce_path_segments_none_is_empty_tree(self):
        summary = SchemaSummary.model_validate(None)
        assert summary.schemas == []
        assert summary.children == {}

    def test_coerce_path_segments_non_dict_passthrough_raises(self):
        with pytest.raises(pydantic.ValidationError):
            SchemaSummary.model_validate([])

    def test_coerce_path_segments_when_children_not_a_dict(self):
        """When ``children`` is present but not a mapping, treat path segments as children."""
        summary = SchemaSummary.model_validate(
            {
                "schemas": [],
                "children": [],
            }
        )
        assert summary.schemas == []
        assert summary.children == {}

    def test_coerce_path_segments_as_wire_shape(self):
        data = {
            "schemas": [],
            "aws": {
                "schemas": [
                    {
                        "name": "aws/cloudtrail",
                        "description": "trail",
                        "current": "1.0.0",
                        "versions": ["1.0.0"],
                        "updated_at": "2026-01-01T00:00:00Z",
                        "column_count": 2,
                    }
                ]
            },
        }
        summary = SchemaSummary.model_validate(data)
        assert summary.schemas == []
        assert "aws" in summary.children
        assert summary.children["aws"].schemas[0].name == "aws/cloudtrail"

    def test_objects_from_list_nested(self):
        objs = [
            SchemaSummaryObject(
                name="aws/cloudtrail",
                description="d",
                current="1",
                versions=["1"],
                updated_at="",
                column_count=3,
            ),
            SchemaSummaryObject(
                name="azure/activity_log",
                description="a",
                current="2",
                versions=["2"],
                updated_at="",
                column_count=1,
            ),
        ]
        tree = SchemaSummary.objects_from_list(objs)
        assert tree.children["aws"].schemas
        assert tree.children["aws"].schemas[0].name == "aws/cloudtrail"
        assert tree.children["azure"].schemas[0].name == "azure/activity_log"

    def test_objects_from_list_deep_segments(self):
        objs = [
            SchemaSummaryObject(
                name="aws/sub/logs",
                description="",
                current="1",
                versions=["1"],
                updated_at="",
                column_count=0,
            )
        ]
        tree = SchemaSummary.objects_from_list(objs)
        assert "aws" in tree.children
        assert "sub" in tree.children["aws"].children
        leaf = tree.children["aws"].children["sub"]
        assert leaf.schemas
        assert leaf.schemas[0].name == "aws/sub/logs"

    def test_objects_from_list_skips_empty_relative_name(self):
        objs = [
            SchemaSummaryObject(
                name="///",
                description="",
                current="1",
                versions=["1"],
                updated_at="",
                column_count=0,
            )
        ]
        tree = SchemaSummary.objects_from_list(objs)
        assert tree.schemas == []
        assert tree.children == {}


class TestPaginatedSchemaSummaryResponse:
    def test_from_summaries_pagination_and_tree(self):
        objs = [
            SchemaSummaryObject(
                name="a/first",
                description="",
                current="1",
                versions=["1"],
                updated_at="",
                column_count=0,
            ),
            SchemaSummaryObject(
                name="b/second",
                description="",
                current="1",
                versions=["1"],
                updated_at="",
                column_count=0,
            ),
        ]
        resp = PaginatedSchemaSummaryResponse.from_summaries(objs, page=1, per_page=1)
        assert resp.total == 2
        assert len(resp.items) == 1
        assert resp.schema_objects.children.keys() >= {"a", "b"}
