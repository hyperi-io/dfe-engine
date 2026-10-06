"""Tests for source ClickHouse plan/deploy artifacts."""

from __future__ import annotations

from pathlib import Path

from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2, SchemaBuildResult
from dfe_engine.source.deployment import (
    SchemaDeployResult,
    SourceDeployDocument,
    SourceDeploymentStore,
    SourcePlanArtifact,
    artifact_from_build,
    deploy_statements_for_build,
    plan_from_build,
    plan_ready_status,
    previous_deployed_version_ids,
    qualify_ddl_statements,
)
from dfe_engine.source.models import SchemaColumn, Source
from dfe_engine.source.type_registry import TypeRegistry
from dfe_engine.yaml_utils import yaml_dump, yaml_load


class TestQualifyDdl:
    def test_replaces_db_placeholder(self):
        out = qualify_ddl_statements(["CREATE TABLE {db}.t (x UInt8)"], "analytics")
        assert out == ["CREATE TABLE analytics.t (x UInt8)"]


class TestSourceDeploymentStore:
    def test_round_trip_plan(self, tmp_path: Path):
        store = SourceDeploymentStore(
            builds_dir=tmp_path / "builds",
            plans_dir=tmp_path / "plans",
            deploys_dir=tmp_path / "deploys",
        )
        plan = SourcePlanArtifact(
            source_name="syslog",
            version="1.0.0",
            planned_at="2026-01-01T00:00:00+00:00",
            statements=["CREATE TABLE analytics.syslog (x UInt8)"],
            ready=True,
        )
        source = Source.model_validate(
            {
                "source": "syslog",
                "enabled": True,
                "match": {"field": "f", "value": "v"},
                "current": "1.0.0",
                "versions": {"1.0.0": {"date_time": "2026-01-01"}},
            }
        )
        store.save_plan(plan, source)
        loaded = store.load_plan("syslog", "1.0.0")
        assert loaded is not None
        assert loaded.statements == plan.statements
        assert loaded.ready is True
        assert (tmp_path / "plans" / "syslog.yaml").is_file()
        doc = yaml_load(tmp_path / "plans" / "syslog.yaml")
        assert doc["source"] == "syslog"
        assert "1.0.0" in doc["versions"]

    def test_delete_build_removes_version(self, tmp_path: Path):
        store = SourceDeploymentStore(
            builds_dir=tmp_path / "builds",
            plans_dir=tmp_path / "plans",
            deploys_dir=tmp_path / "deploys",
        )
        source = Source.model_validate(
            {
                "source": "syslog",
                "match": {"field": "f", "value": "v"},
                "current": "2.0.0",
                "deployed_version": "1.0.0",
                "versions": {
                    "1.0.0": {"date_time": "2026-01-01"},
                    "2.0.0": {"date_time": "2026-01-02"},
                },
            }
        )
        art = artifact_from_build(
            SchemaBuildResult(
                source_name="syslog",
                columns=[],
                create_table_ddl="CREATE TABLE t",
            ),
            version="2.0.0",
        )
        store.save_build(art, source)
        assert store.load_build("syslog", "2.0.0") is not None
        assert store.delete_build("syslog", "2.0.0", source=source) is True
        assert store.load_build("syslog", "2.0.0") is None
        assert not (tmp_path / "builds" / "syslog.yaml").is_file()

    def test_previous_deployed_version_ids_excludes_live(self):
        source = Source.model_validate(
            {
                "source": "x",
                "match": {"field": "f", "value": "v"},
                "deployed_version": "2.0.0",
                "current": "3.0.0",
                "versions": {
                    "1.0.0": {"date_time": "2026-01-01"},
                    "2.0.0": {"date_time": "2026-01-02"},
                    "3.0.0": {"date_time": "2026-01-03"},
                },
            }
        )
        doc = SourceDeployDocument(
            source="x",
            deployed_version="2.0.0",
            versions={
                "1.0.0": SchemaDeployResult(
                    source_name="x",
                    version="1.0.0",
                    dry_run=False,
                    applied=True,
                    create_table="CREATE TABLE x_v1",
                ),
                "2.0.0": SchemaDeployResult(
                    source_name="x",
                    version="2.0.0",
                    dry_run=False,
                    applied=True,
                    create_table="CREATE TABLE x_v2",
                ),
            },
        )
        assert previous_deployed_version_ids(source, doc) == ["1.0.0"]

    def test_round_trip_deploy_result(self, tmp_path: Path):
        store = SourceDeploymentStore(
            builds_dir=tmp_path / "builds",
            plans_dir=tmp_path / "plans",
            deploys_dir=tmp_path / "deploys",
        )
        source = Source.model_validate(
            {
                "source": "syslog",
                "enabled": True,
                "match": {"field": "f", "value": "v"},
                "current": "1.0.0",
                "versions": {"1.0.0": {"date_time": "2026-01-01"}},
            }
        )
        result = SchemaDeployResult(
            source_name="syslog",
            version="1.0.0",
            dry_run=False,
            applied=True,
            create_table="CREATE TABLE dfe.syslog (x UInt8)",
            views={"sigma": "CREATE VIEW dfe.v AS SELECT 1"},
            statements_applied=2,
        )
        store.save_deploy(result, source)
        loaded = store.load_deploy("syslog", "1.0.0")
        assert loaded == result
        doc = yaml_load(tmp_path / "deploys" / "syslog.yaml")
        assert doc["deployed_version"] == "1.0.0"
        assert doc["versions"]["1.0.0"]["create_table"].startswith("CREATE TABLE")
        assert doc["versions"]["1.0.0"]["applied"] is True

    def test_migrates_legacy_per_version_files(self, tmp_path: Path):
        store = SourceDeploymentStore(
            builds_dir=tmp_path / "builds",
            plans_dir=tmp_path / "plans",
            deploys_dir=tmp_path / "deploys",
        )
        legacy = tmp_path / "plans" / "evt"
        legacy.mkdir(parents=True)
        yaml_dump(
            {
                "source_name": "evt",
                "version": "1.0.0",
                "planned_at": "2026-01-01T00:00:00+00:00",
                "ready": True,
                "statements": ["SELECT 1"],
            },
            legacy / "1.0.0.yaml",
        )
        loaded = store.load_plan("evt", "1.0.0")
        assert loaded is not None
        assert loaded.statements == ["SELECT 1"]
        assert (tmp_path / "plans" / "evt.yaml").is_file()


class TestDeployStatements:
    def test_new_table_uses_create(self):
        source = Source.model_validate(
            {
                "source": "evt",
                "enabled": True,
                "match": {"field": "f", "value": "v"},
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "schema": {"engine": "MergeTree"},
                    }
                },
            }
        )
        result = SchemaBuildResult(
            source_name="evt",
            columns=[SchemaColumn(name="alpha", type="string")],
            create_table_ddl="CREATE TABLE {db}.evt (`alpha` String)",
            view_ddls={},
        )
        builder = SchemaBuilderV2(TypeRegistry.default())
        statements, exists = deploy_statements_for_build(
            builder,
            source,
            "1.0.0",
            result,
            db="analytics",
            ch_client=None,
        )
        assert exists is False
        assert len(statements) == 1
        assert "analytics.evt" in statements[0]

    def test_plan_ready_false_on_validation_errors(self):
        result = SchemaBuildResult(
            source_name="x",
            columns=[],
            create_table_ddl="",
            validation_errors=["bad column"],
        )
        plan = plan_from_build(result, version="1.0.0", statements=[], table_exists=False)
        assert plan.ready is False
        assert plan.validation_errors == ["bad column"]
        assert "validation failed" in (plan.ready_reason or "").lower()

    def test_plan_ready_when_table_in_sync(self):
        # A routing or transform change leaves the columns alone and must still deploy.
        result = SchemaBuildResult(
            source_name="x",
            columns=[],
            create_table_ddl="CREATE TABLE {db}.x (`a` String)",
        )
        plan = plan_from_build(result, version="1.0.0", statements=[], table_exists=True)
        assert plan.ready is True
        assert plan.ready_reason is not None
        assert "already matches" in plan.ready_reason

    def test_plan_not_ready_without_ddl_or_table(self):
        result = SchemaBuildResult(source_name="x", columns=[], create_table_ddl="")
        plan = plan_from_build(result, version="1.0.0", statements=[], table_exists=False)
        assert plan.ready is False
        assert "No deploy DDL" in (plan.ready_reason or "")

    def test_plan_ready_status_helper(self):
        ready, reason = plan_ready_status(
            validation_errors=[],
            statements=["ALTER TABLE t ADD COLUMN x UInt8"],
            table_exists=True,
        )
        assert ready is True
        assert "1 DDL" in reason

    def test_artifact_from_build(self):
        result = SchemaBuildResult(
            source_name="x",
            columns=[SchemaColumn(name="a", type="string")],
            create_table_ddl="CREATE TABLE {db}.x (`a` String)",
        )
        art = artifact_from_build(result, version="2.0.0")
        assert art.version == "2.0.0"
        assert art.column_count == 1
