"""Tests for SchemaRegistry (meta-schema YAML directory)."""

from pathlib import Path

import pydantic
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
    )


class TestSchemaRegistryCRUD:
    def test_get_not_found(self, registry):
        with pytest.raises(SchemaNotFoundError):
            registry.get_schema("missing/schema")

    def test_save_and_get_nested_path(self, registry):
        ms = _minimal_meta("aws/cloudtrail")
        registry.save_schema(ms)
        loaded = registry.get_schema("aws/cloudtrail")
        assert loaded.current == "1"
        assert loaded.path == "aws/cloudtrail"

    def test_save_three_level_path_writes_yaml_beside_an_existing_directory(
        self, registry, schemas_dir
    ):
        """Create meta/test/test as a YAML file even if meta/test/test is already a dir."""
        colliding_dir = schemas_dir / "meta" / "test" / "test"
        colliding_dir.mkdir(parents=True)
        registry.save_schema(_minimal_meta("meta/test/test"))
        yaml_file = schemas_dir / "meta" / "test" / "test.yaml"
        assert yaml_file.is_file()
        assert colliding_dir.is_dir()
        loaded = registry.get_schema("meta/test/test")
        assert loaded.current == "1"

    def test_save_flat_table_key(self, registry):
        ms = _minimal_meta("standalone")
        registry.save_schema(ms)
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
            registry.save_schema(ms)

    def test_save_dict_without_path_raises(self, registry):
        with pytest.raises(SchemaValidationError):
            registry.save_schema(
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

    def test_get_schema_current_version_reads_raw_yaml(self, registry, schemas_dir):
        bad = schemas_dir / "meta" / "m365" / "alerts.yaml"
        bad.parent.mkdir(parents=True)
        bad.write_text(
            "current: '2.0.0'\nversions:\n  '1.0.0':\n"
            "    date: '2026-01-01'\n    type: model\n    summary: init\n"
            "    columns:\n      - {name: a, type: string, expr: '@a'}\n"
            "  '2.0.0':\n    date: '2026-06-12'\n    type: model\n"
            "    summary: stub\n    columns: []\n",
            encoding="utf-8",
        )
        registry._store._refresh_all()
        assert registry.get_schema_current_version("meta/m365/alerts") == "2.0.0"
        with pytest.raises(pydantic.ValidationError, match="must contain at least 1 element"):
            registry.get_schema("meta/m365/alerts")

    def test_list_schemas_metadata(self, registry):
        registry.save_schema(_minimal_meta("aws/cloudtrail"))
        rows = registry.list_schemas()
        assert len(rows) == 1
        row = rows[0]
        assert row["path"] == "aws/cloudtrail"
        assert row["current"] == "1"
        assert row["versions"] == ["1"]
        assert row["column_count"] == 1
        assert "description" not in row

    def test_delete_schema(self, registry):
        registry.save_schema(_minimal_meta("tmp/log"))
        registry.delete_schema("tmp/log")
        with pytest.raises(SchemaNotFoundError):
            registry.get_schema("tmp/log")

    def test_find_schema_at_location(self, registry):
        assert registry.find_schema_at_location("aws/cloudtrail") is None
        registry.save_schema(_minimal_meta("aws/cloudtrail"))
        assert registry.find_schema_at_location("aws/cloudtrail") == "aws/cloudtrail"
        assert registry.find_schema_at_location("aws//cloudtrail") == "aws/cloudtrail"

    def test_canonical_schema_path(self):
        from dfe_engine.schema.registry import canonical_schema_path

        assert canonical_schema_path("aws/cloudtrail") == "aws/cloudtrail"
        assert canonical_schema_path("aws//cloudtrail") == "aws/cloudtrail"
        assert canonical_schema_path("aws\\cloudtrail") == "aws/cloudtrail"
        assert canonical_schema_path("standalone") == "standalone"

    def test_relative_schemas_directory_is_resolved(self, tmp_path, monkeypatch):
        """A relative schemas directory resolves, so every schema path is absolute."""
        monkeypatch.chdir(tmp_path)
        schemas = tmp_path / "schemas"
        schemas.mkdir()
        SchemaRegistry.reset_instance()
        reg = SchemaRegistry(schemas_directory="schemas", writable=True, refresh_interval=0)
        try:
            assert reg._directory == schemas.resolve()
            p = reg._yaml_path("a/b")
            assert p.is_absolute()
        finally:
            reg.close()
            SchemaRegistry.reset_instance()


class TestSchemaRegistryPathSafety:
    def test_yaml_path_rejects_dotdot_segment(self, registry):
        with pytest.raises(SchemaValidationError, match="segment"):
            registry._yaml_path("aws/../escape")

    def test_yaml_path_rejects_dot_segment(self, registry):
        with pytest.raises(SchemaValidationError, match="segment"):
            registry._yaml_path("aws/./cloudtrail")

    def test_yaml_path_rejects_empty_after_normalization(self, registry):
        with pytest.raises(SchemaValidationError, match="empty"):
            registry._yaml_path("///")

    def test_yaml_path_accepts_backslash_as_separator(self, registry):
        p = registry._yaml_path("aws\\cloudtrail")
        assert p.name == "cloudtrail.yaml"
        assert p.parent.name == "aws"

    def test_save_two_schemas_same_basename_different_prefix(self, registry):
        registry.save_schema(_minimal_meta("acme/cloudtrail"))
        registry.save_schema(_minimal_meta("contoso/cloudtrail"))
        assert registry.get_schema("acme/cloudtrail").current == "1"
        assert registry.get_schema("contoso/cloudtrail").current == "1"

    def test_save_schema_rejects_traversal(self, registry):
        with pytest.raises(SchemaValidationError, match="segment"):
            registry.save_schema(_minimal_meta("../../../etc/passwd"))

    @pytest.mark.parametrize("path", ["registries/engines", "registries\\types"])
    def test_yaml_path_rejects_the_registries_tree(self, path, registry):
        with pytest.raises(SchemaValidationError, match="registries/"):
            registry._yaml_path(path)


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


class TestSchemaRegistryCoverage:
    """Extra branches for meta-schema registry behaviour."""

    def test_yaml_path_rejects_null_byte(self, registry):
        with pytest.raises(SchemaValidationError, match="null"):
            registry._yaml_path("bad\0/name")

    def test_yaml_path_rejects_escape_via_symlink(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        schemas = tmp_path / "schemas"
        schemas.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (schemas / "evil").symlink_to(outside, target_is_directory=True)
        SchemaRegistry.reset_instance()
        reg = SchemaRegistry(schemas_directory=schemas, writable=True, refresh_interval=0)
        try:
            with pytest.raises(SchemaValidationError, match="escapes"):
                reg._yaml_path("evil/nested")
        finally:
            reg.close()
            SchemaRegistry.reset_instance()

    def test_save_schema_rejects_invalid_dict(self, registry):
        with pytest.raises(SchemaValidationError, match="Invalid schema"):
            registry.save_schema(
                {
                    "path": "bad/doc",
                    "current": "1",
                    "versions": "not-a-mapping",
                }
            )

    def test_delete_schema_no_file_is_safe(self, registry):
        registry.delete_schema("missing/file")

    def test_list_schemas_filters_exact_path(self, registry):
        registry.save_schema(_minimal_meta("a/one"))
        registry.save_schema(_minimal_meta("b/two"))
        rows = registry.list_schemas(path="a/one")
        assert len(rows) == 1
        assert rows[0]["path"] == "a/one"

    def test_list_schemas_skips_when_cache_miss(self, registry, schemas_dir):
        """Unparseable YAML appears in glob but never loads into cache."""
        bad = schemas_dir / "broken.yaml"
        bad.write_text(": [this is not valid yaml ::\n", encoding="utf-8")
        registry._store._refresh_all()
        registry.save_schema(_minimal_meta("good/ok"))
        rows = registry.list_schemas()
        paths = {r["path"] for r in rows}
        assert "good/ok" in paths
        assert "broken" not in paths

    def test_list_schemas_skips_invalid_meta_schema(self, registry, schemas_dir):
        bad = schemas_dir / "invalid_meta.yaml"
        bad.write_text("current: '1'\n", encoding="utf-8")
        registry._store._refresh_all()
        registry.save_schema(_minimal_meta("good/meta"))
        rows = registry.list_schemas()
        paths = {r["path"] for r in rows}
        assert "invalid_meta" not in paths
        assert "good/meta" in paths

    def test_list_schemas_column_count_uses_current_version_columns(self, registry):
        """Listing reports the column count of the schema's current version."""
        ms = MetaSchema(
            path="orphan/v",
            current="1",
            versions={
                "1": SchemaVersion(
                    date="2026-01-01",
                    type="model",
                    summary="init",
                    columns=[
                        SchemaColumn(name="a", type="string", expr="@a"),
                        SchemaColumn(name="b", type="string", expr="@b"),
                    ],
                )
            },
        )
        registry.save_schema(ms)
        rows = registry.list_schemas()
        row = next(r for r in rows if r["path"] == "orphan/v")
        assert row["column_count"] == 2

    def test_schema_with_empty_versions_is_rejected(self):
        """A schema with no versions is rejected at construction (never listed)."""
        with pytest.raises(
            pydantic.ValidationError, match="'versions' must contain at least 1 element"
        ):
            MetaSchema(
                path="empty/vers",
                current="1",
                versions={},
            )

    def test_list_schemas_skips_current_not_in_versions(self, registry, schemas_dir):
        """Invalid on-disk YAML (current missing from versions) is excluded from list."""
        bad = schemas_dir / "orphan" / "bad.yaml"
        bad.parent.mkdir(parents=True)
        bad.write_text(
            "current: missing\nversions:\n  '1':\n"
            "  date: '2026-01-01'\n  type: model\n  summary: init\n"
            "  columns:\n    - {name: a, type: string, expr: '@a'}\n",
            encoding="utf-8",
        )
        registry._store._refresh_all()
        paths = {r["path"] for r in registry.list_schemas()}
        assert "orphan/bad" not in paths

    def test_list_schemas_updated_at_empty_on_stat_failure(self, registry, monkeypatch):
        registry.save_schema(_minimal_meta("stat/break"))
        orig_yaml_path = registry._yaml_path

        class _StatDeniedPath:
            """Delegate to a real path but fail ``stat()`` (list_schemas updated_at only)."""

            def __init__(self, path: Path) -> None:
                self._path = path

            def stat(self, *args, **kwargs):
                raise OSError("stat denied")

            def __getattr__(self, name: str):
                return getattr(self._path, name)

        def yaml_path_with_stat_failure(table: str) -> Path:
            path = orig_yaml_path(table)
            if table == "stat/break":
                return _StatDeniedPath(path)  # ty: ignore[invalid-return-type]  # a stand-in path whose stat is refused
            return path

        monkeypatch.setattr(registry, "_yaml_path", yaml_path_with_stat_failure)
        rows = registry.list_schemas()
        row = next(r for r in rows if r["path"] == "stat/break")
        assert row["updated_at"] == ""

    def test_on_change_registers_callback(self, registry):
        def cb(table: str, data: dict) -> None:
            del table, data

        registry.on_change("some/table", cb)
        assert registry._store._watchers["some/table"] == [cb]

    def test_close_stops_background_refresh(self, registry):
        registry.close()
        assert registry._store._refresh_thread is None

    def test_reset_instance_when_none_is_safe(self):
        SchemaRegistry.reset_instance()
        SchemaRegistry.reset_instance()
        assert SchemaRegistry._instance is None
