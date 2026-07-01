"""Tests for core resource mutation guards."""

from __future__ import annotations

from dfe_engine.core_resources.policy import (
    core_schema_current_only_patch,
    fieldmap_target_is_core,
    match_api_core_mutation,
    schema_registry_path_is_core,
)
from dfe_engine.core_resources.yaml_resource_type import config_is_core
from dfe_engine.fieldmap.registry import FieldMapRegistry
from dfe_engine.schema.registry import SchemaRegistry
from dfe_engine.yaml_utils import yaml_dump


class TestYamlResourceType:
    def test_core_from_yaml(self):
        assert config_is_core({"resource_type": "core"})


class TestMatchApiCoreMutation:
    def test_blocks_put_on_core_role(self, tmp_path):
        from dfe_engine.auth.role_store import RoleStore

        path = tmp_path / "roles.yaml"
        yaml_dump(
            {
                "roles": {
                    "admin": {
                        "description": "",
                        "permissions": ["*"],
                        "resource_type": "core",
                    }
                }
            },
            path,
        )
        store = RoleStore(path)
        msg = match_api_core_mutation(
            method="PUT",
            path="/api/v1/auth/roles/admin",
            role_store=store,
            schema_registry=None,
        )
        assert msg == "Core resources can't be mutated"


class TestSchemaRegistryPathIsCore:
    def test_reads_resource_type_from_store(self, tmp_path):
        schemas_dir = tmp_path / "schemas"
        path = schemas_dir / "common-header" / "minimal.yaml"
        path.parent.mkdir(parents=True)
        yaml_dump(
            {
                "resource_type": "core",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-01",
                        "type": "model",
                        "summary": "x",
                        "columns": [{"name": "a", "type": "string"}],
                    }
                },
            },
            path,
        )
        reg = SchemaRegistry(schemas_directory=schemas_dir, refresh_interval=0)
        assert schema_registry_path_is_core("common-header/minimal", reg) is True
        reg.close()


class TestCoreSchemaPatchCurrent:
    def _core_registry(self, tmp_path) -> SchemaRegistry:
        schemas_dir = tmp_path / "schemas"
        path = schemas_dir / "common-header" / "minimal.yaml"
        path.parent.mkdir(parents=True)
        yaml_dump(
            {
                "resource_type": "core",
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-01",
                        "type": "model",
                        "summary": "x",
                        "columns": [{"name": "a", "type": "string"}],
                    },
                    "1.1.0": {
                        "date": "2026-02-01",
                        "type": "addition",
                        "summary": "y",
                        "columns": [{"name": "a", "type": "string"}],
                    },
                },
            },
            path,
        )
        return SchemaRegistry(schemas_directory=schemas_dir, refresh_interval=0)

    def test_allows_patch_current_only(self, tmp_path):
        reg = self._core_registry(tmp_path)
        try:
            msg = match_api_core_mutation(
                method="PATCH",
                path="/api/v1/schemas/definitions/common-header/minimal",
                role_store=None,
                schema_registry=reg,
                body=b'{"current": "1.1.0"}',
            )
            assert msg is None
        finally:
            reg.close()

    def test_blocks_patch_summary(self, tmp_path):
        reg = self._core_registry(tmp_path)
        try:
            msg = match_api_core_mutation(
                method="PATCH",
                path="/api/v1/schemas/definitions/common-header/minimal",
                role_store=None,
                schema_registry=reg,
                body=b'{"summary": "nope"}',
            )
            assert msg == "Core resources can't be mutated"
        finally:
            reg.close()

    def test_blocks_patch_current_and_summary(self, tmp_path):
        reg = self._core_registry(tmp_path)
        try:
            msg = match_api_core_mutation(
                method="PATCH",
                path="/api/v1/schemas/definitions/common-header/minimal",
                role_store=None,
                schema_registry=reg,
                body=b'{"current": "1.1.0", "summary": "nope"}',
            )
            assert msg == "Core resources can't be mutated"
        finally:
            reg.close()

    def test_blocks_post_new_version(self, tmp_path):
        reg = self._core_registry(tmp_path)
        try:
            msg = match_api_core_mutation(
                method="POST",
                path="/api/v1/schemas/definitions/common-header/minimal/versions",
                role_store=None,
                schema_registry=reg,
                body=b"{}",
            )
            assert msg == "Core resources can't be mutated"
        finally:
            reg.close()

    def test_core_schema_current_only_patch_helper(self):
        assert core_schema_current_only_patch(b'{"current": "1.1.0"}') is True
        assert core_schema_current_only_patch(b'{"current": "1.1.0", "summary": "x"}') is False
        assert core_schema_current_only_patch(b'{"summary": "x"}') is False
        assert core_schema_current_only_patch(b"{}") is False


class TestFieldmapTargetIsCore:
    def test_reads_resource_type_from_file(self, tmp_path):
        maps_dir = tmp_path / "maps"
        maps_dir.mkdir()
        sub = maps_dir / "sigma"
        sub.mkdir()
        yaml_dump(
            {
                "resource_type": "core",
                "standard": "sigma",
                "mappings": {"a": "b"},
            },
            sub / "_default.yaml",
        )
        reg = FieldMapRegistry(field_maps_directory=maps_dir, refresh_interval=0)
        assert fieldmap_target_is_core("sigma", None, reg) is True
        reg.close()
