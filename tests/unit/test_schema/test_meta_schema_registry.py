"""Tests for SchemaRegistry (meta-schema YAML directory)."""

from __future__ import annotations

import pytest

from dfe_engine.schema.models import MetaSchema, SchemaColumn, SchemaVersion
from dfe_engine.schema.registry import (
    SchemaError,
    SchemaNotFoundError,
    SchemaRegistry,
    SchemaValidationError,
)


@pytest.fixture
def schemas_dir(tmp_path):
    d = tmp_path / "schemas"
    d.mkdir()
    return d


@pytest.fixture
def registry(schemas_dir):
    SchemaRegistry.reset_instance()
    reg = SchemaRegistry(schemas_directory=schemas_dir, writable=True, refresh_interval=0)
    yield reg
    reg.close()
    SchemaRegistry.reset_instance()


def _minimal_meta(path: str) -> MetaSchema:
    return MetaSchema(
        path=path,
        current="1",
        versions={
            "1": SchemaVersion(
                date="2026-01-01",
                type="model",
                summary="init",
                columns=[
                    SchemaColumn(name="event_id", type="string", expr="@source: id"),
                ],
            )
        },
        description="test schema",
    )


class TestSchemaRegistryCRUD:
    def test_get_not_found(self, registry):
        with pytest.raises(SchemaNotFoundError):
            registry.get_schema("missing/schema")

    def test_save_and_get_nested_path(self, registry):
        ms = _minimal_meta("aws/cloudtrail")
        registry.save_map(ms)
        loaded = registry.get_schema("aws/cloudtrail")
        assert loaded.current == "1"
        assert loaded.description == "test schema"
        assert loaded.path is None

    def test_save_flat_table_key(self, registry):
        ms = _minimal_meta("standalone")
        registry.save_map(ms)
        loaded = registry.get_schema("standalone")
        assert loaded.current == "1"

    def test_save_requires_path(self, registry):
        ms = MetaSchema(
            path=None,
            current="1",
            versions={
                "1": SchemaVersion(
                    date="2026-01-01",
                    type="model",
                    summary="init",
                    columns=[
                        SchemaColumn(name="a", type="string", expr="@source: a"),
                    ],
                )
            },
        )
        with pytest.raises(SchemaValidationError, match="path"):
            registry.save_map(ms)

    def test_save_dict_without_path_raises(self, registry):
        with pytest.raises(SchemaValidationError):
            registry.save_map(
                {
                    "current": "1",
                    "versions": {
                        "1": {
                            "date": "2026-01-01",
                            "type": "model",
                            "summary": "init",
                            "columns": [{"name": "a", "type": "string", "expr": "@x"}],
                        }
                    },
                }
            )

    def test_list_schemas_metadata(self, registry):
        registry.save_map(_minimal_meta("aws/cloudtrail"))
        rows = registry.list_schemas()
        assert len(rows) == 1
        row = rows[0]
        assert row["path"] == "aws/cloudtrail"
        assert row["current"] == "1"
        assert row["versions"] == ["1"]
        assert row["column_count"] == 1
        assert row["description"] == "test schema"

    def test_delete_schema(self, registry):
        registry.save_map(_minimal_meta("tmp/log"))
        registry.delete_schema("tmp/log")
        with pytest.raises(SchemaNotFoundError):
            registry.get_schema("tmp/log")


class TestSchemaRegistrySingleton:
    def test_get_instance_requires_directory_first_call(self):
        SchemaRegistry.reset_instance()
        with pytest.raises(SchemaError, match="schemas_directory"):
            SchemaRegistry.get_instance()
        SchemaRegistry.reset_instance()

    def test_get_instance_returns_same_object(self, schemas_dir):
        SchemaRegistry.reset_instance()
        a = SchemaRegistry.get_instance(schemas_directory=schemas_dir, refresh_interval=0)
        b = SchemaRegistry.get_instance()
        assert a is b
        a.close()
        SchemaRegistry.reset_instance()
