"""Tests for source ClickHouse plan/deploy artifacts."""

from __future__ import annotations

from pathlib import Path

from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2, SchemaBuildResult
from dfe_engine.source.deployment import (
    SourceDeploymentStore,
    SourcePlanArtifact,
    artifact_from_build,
    deploy_statements_for_build,
    plan_from_build,
    qualify_ddl_statements,
)
from dfe_engine.source.models import SchemaColumn, Source
from dfe_engine.source.type_registry import TypeRegistry


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
        store.save_plan(plan)
        loaded = store.load_plan("syslog", "1.0.0")
        assert loaded is not None
        assert loaded.statements == plan.statements
        assert loaded.ready is True


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

    def test_artifact_from_build(self):
        result = SchemaBuildResult(
            source_name="x",
            columns=[SchemaColumn(name="a", type="string")],
            create_table_ddl="CREATE TABLE {db}.x (`a` String)",
        )
        art = artifact_from_build(result, version="2.0.0")
        assert art.version == "2.0.0"
        assert art.column_count == 1
