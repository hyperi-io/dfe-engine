"""Tests for meta-schema API models (SchemaSummary tree + MetaSchema)."""

from __future__ import annotations

import pydantic
import pytest

from dfe_engine.schema.models import (
    MetaSchema,
    MetaSchemaAddVersionRequest,
    PaginatedSchemaSummaryResponse,
    SchemaColumn,
    SchemaColumnWrite,
    SchemaSummary,
    SchemaSummaryObject,
    SchemaVersion,
)
from dfe_engine.schema.registry import coerce_common_header_legacy_versions


@pytest.fixture
def cloudtrail_like_yaml() -> dict:
    return {
        "current": "1.0.0",
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
        assert len(ms.versions["1.0.0"].columns) == 1

    def test_validate_ignores_legacy_description_key(self, cloudtrail_like_yaml: dict):
        ms = MetaSchema.model_validate({**cloudtrail_like_yaml, "description": "legacy"})
        assert "description" not in MetaSchema.model_fields
        assert ms.versions["1.0.0"].columns[0].name == "event_id"

    def test_to_yaml_dict_omits_path(self, cloudtrail_like_yaml: dict):
        ms = MetaSchema.model_validate(
            {**cloudtrail_like_yaml, "path": "aws/cloudtrail"},
        )
        dumped = ms.to_yaml_dict()
        assert "path" not in dumped
        assert dumped["current"] == "1.0.0"

    def test_to_yaml_dict_omits_empty_column_strings(self):
        ms = MetaSchema(
            current="1",
            versions={
                "1": SchemaVersion(
                    date="2026-01-01",
                    type="model",
                    summary="init",
                    columns=[
                        SchemaColumn(
                            name="event_id",
                            type="string",
                            use_case="",
                            expr="@source: EventId",
                            comment="",
                        ),
                    ],
                )
            },
        )
        col = ms.to_yaml_dict()["versions"]["1"]["columns"][0]
        assert "use_case" not in col
        assert "comment" not in col
        assert col["name"] == "event_id"

    def test_schema_column_rejects_empty_name(self):
        with pytest.raises(pydantic.ValidationError, match="'name' must be a non-empty string"):
            SchemaColumn(name="", type="string")

        with pytest.raises(pydantic.ValidationError, match="'name' must be a non-empty string"):
            SchemaColumn(name="   ", type="string")

    def test_schema_column_rejects_empty_name_in_meta_schema(self, cloudtrail_like_yaml: dict):
        bad = {
            **cloudtrail_like_yaml,
            "versions": {
                "1.0.0": {
                    **cloudtrail_like_yaml["versions"]["1.0.0"],
                    "columns": [{"name": "", "type": "string", "expr": "@source: x"}],
                }
            },
        }
        with pytest.raises(pydantic.ValidationError, match="'name' must be a non-empty string"):
            MetaSchema.model_validate(bad)

    def test_every_version_must_define_columns_even_after_legacy_coercion(self):
        stub_yaml = {
            "current": "1.1.0",
            "versions": {
                "1.0.0": {
                    "date": "2026-01-15",
                    "type": "model",
                    "summary": "Initial stub",
                },
                "1.1.0": {
                    "date": "2026-04-10",
                    "type": "model",
                    "summary": "Full definition",
                    "columns": [
                        {"name": "event_id", "type": "string", "expr": "@source: id"},
                    ],
                },
            },
        }
        with pytest.raises(pydantic.ValidationError, match="columns"):
            MetaSchema.model_validate(stub_yaml)

        # Legacy coercion fills missing columns with [], but the NonEmptyList
        # validator on SchemaVersion.columns now rejects empty columns on every
        # version - including coerced legacy stubs.
        with pytest.raises(
            pydantic.ValidationError, match="'columns' must contain at least 1 element"
        ):
            MetaSchema.model_validate(
                coerce_common_header_legacy_versions("common-header/minimal", stub_yaml)
            )

    def test_current_version_must_have_columns(self):
        with pytest.raises(
            pydantic.ValidationError,
            match=r"'columns' must contain at least 1 element",
        ):
            MetaSchema.model_validate(
                coerce_common_header_legacy_versions(
                    "common-header/minimal",
                    {
                        "current": "1.0.0",
                        "versions": {
                            "1.0.0": {
                                "date": "2026-01-15",
                                "type": "model",
                                "summary": "Stub without columns",
                            },
                        },
                    },
                )
            )

    def test_meta_path_rejects_empty_columns_on_current(self):
        with pytest.raises(
            pydantic.ValidationError,
            match=r"'columns' must contain at least 1 element",
        ):
            MetaSchema.model_validate(
                {
                    "current": "2.0.0",
                    "path": "meta/m365/alerts",
                    "versions": {
                        "2.0.0": {
                            "date": "2026-06-12",
                            "type": "model",
                            "summary": "stub",
                            "columns": [],
                        },
                    },
                },
            )


class TestSchemaSummaryTree:
    def test_coerce_path_segments_none_is_empty_tree(self):
        summary = SchemaSummary.model_validate(None)
        assert summary.items == []
        assert summary.children == {}

    def test_coerce_path_segments_non_dict_passthrough_raises(self):
        with pytest.raises(pydantic.ValidationError):
            SchemaSummary.model_validate([])

    def test_coerce_path_segments_when_children_not_a_dict(self):
        """When ``children`` is present but not a mapping, treat path segments as children."""
        summary = SchemaSummary.model_validate(
            {
                "items": [],
                "children": [],
            }
        )
        assert summary.items == []
        assert summary.children == {}

    def test_coerce_path_segments_as_wire_shape(self):
        data = {
            "items": [],
            "aws": {
                "items": [
                    {
                        "name": "aws/cloudtrail",
                        "current": "1.0.0",
                        "versions": ["1.0.0"],
                        "updated_at": "2026-01-01T00:00:00Z",
                        "column_count": 2,
                    }
                ]
            },
        }
        summary = SchemaSummary.model_validate(data)
        assert summary.items == []
        assert "aws" in summary.children
        assert summary.children["aws"].items[0].name == "aws/cloudtrail"

    def test_from_paths_nested(self):
        objs = [
            SchemaSummaryObject(
                name="aws/cloudtrail",
                current="1",
                versions=["1"],
                updated_at="2026-01-01T00:00:00Z",
                column_count=3,
            ),
            SchemaSummaryObject(
                name="azure/activity_log",
                current="2",
                versions=["2"],
                updated_at="2026-01-01T00:00:00Z",
                column_count=1,
            ),
        ]
        tree = SchemaSummary.from_paths(objs, path=lambda o: o.name)
        assert tree.children["aws"].items
        assert tree.children["aws"].items[0].name == "aws/cloudtrail"
        assert tree.children["azure"].items[0].name == "azure/activity_log"

    def test_from_paths_deep_segments(self):
        objs = [
            SchemaSummaryObject(
                name="aws/sub/logs",
                current="1",
                versions=["1"],
                updated_at="2026-01-01T00:00:00Z",
                column_count=1,
            )
        ]
        tree = SchemaSummary.from_paths(objs, path=lambda o: o.name)
        assert "aws" in tree.children
        assert "sub" in tree.children["aws"].children
        leaf = tree.children["aws"].children["sub"]
        assert leaf.items
        assert leaf.items[0].name == "aws/sub/logs"

    def test_from_paths_skips_empty_relative_name(self):
        objs = [
            SchemaSummaryObject(
                name="///",
                current="1",
                versions=["1"],
                updated_at="2026-01-01T00:00:00Z",
                column_count=1,
            )
        ]
        tree = SchemaSummary.from_paths(objs, path=lambda o: o.name)
        assert tree.items == []
        assert tree.children == {}


class TestPaginatedSchemaSummaryResponse:
    def test_from_summaries_pagination_and_tree(self):
        objs = [
            SchemaSummaryObject(
                name="a/first",
                current="1",
                versions=["1"],
                updated_at="2026-01-01T00:00:00Z",
                column_count=1,
            ),
            SchemaSummaryObject(
                name="b/second",
                current="1",
                versions=["1"],
                updated_at="2026-01-01T00:00:00Z",
                column_count=1,
            ),
        ]
        resp = PaginatedSchemaSummaryResponse.from_summaries(objs, page=1, per_page=1)
        assert resp.total == 2
        assert len(resp.items) == 1
        assert resp.objects.children.keys() >= {"a", "b"}


class TestMetaSchemaAddVersionRequest:
    def test_summary_optional(self):
        req = MetaSchemaAddVersionRequest.model_validate(
            {
                "type": "addition",
                "columns": [
                    {
                        "name": "x",
                        "type": "string",
                        "expr": "@source: X",
                        "_field_type": "base",
                    }
                ],
            }
        )
        assert req.summary is None

    def test_summary_accepted_when_provided(self):
        req = MetaSchemaAddVersionRequest(
            type="revision",
            summary="Bump columns",
            columns=[
                SchemaColumnWrite(
                    name="x",
                    type="string",
                    expr="@source: X",
                    field_type="base",
                )
            ],
        )
        assert req.summary == "Bump columns"

    def test_rejects_empty_columns(self):
        with pytest.raises(pydantic.ValidationError, match="at least 1"):
            MetaSchemaAddVersionRequest.model_validate({"type": "model", "columns": []})
