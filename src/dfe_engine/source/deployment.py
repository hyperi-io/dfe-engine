"""Source ClickHouse plan/deploy — build artifacts, dry-run plans, and deploy execution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyperi_pylib.logger import logger
from pydantic import BaseModel, Field

from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2, SchemaBuildResult
from dfe_engine.services.schema.json_promotion_service import clickhouse_table_exists
from dfe_engine.source.models import Source
from dfe_engine.source.type_registry import TypeRegistry
from dfe_engine.yaml_utils import yaml_dump, yaml_load


class SourceBuildArtifact(BaseModel):
    """Persisted output of POST /sources/{name}/build (source-builds)."""

    source_name: str
    version: str
    built_at: str
    validation_errors: list[str] = Field(default_factory=list)
    create_table_ddl: str = ""
    sigma_view_ddl: str | None = None
    view_ddls: dict[str, str] = Field(default_factory=dict)
    column_count: int = 0


class SourcePlanArtifact(BaseModel):
    """Dry-run deploy plan persisted under source-plans."""

    source_name: str
    version: str
    planned_at: str
    table_exists: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    statements: list[str] = Field(default_factory=list)
    create_table_ddl: str = ""
    view_ddls: dict[str, str] = Field(default_factory=dict)
    ready: bool = Field(
        description="True when there are no validation errors and deploy statements are present"
    )


class SourceDeployArtifact(BaseModel):
    """Result of a deploy run persisted under source-deploys."""

    source_name: str
    version: str
    deployed_at: str
    success: bool
    deployed_version: str | None = None
    ddl_executed: list[str] = Field(default_factory=list)
    ddl_failed: list[dict[str, str]] = Field(default_factory=list)


def qualify_ddl_statements(statements: list[str], db: str) -> list[str]:
    """Replace ``{db}`` placeholders with the target data database."""
    return [stmt.replace("{db}", db) for stmt in statements if stmt.strip()]


def _list_table_columns(client: Any, db: str, table: str) -> set[str]:
    try:
        rows = client.execute(
            "SELECT name FROM system.columns WHERE database = {db:String} AND table = {tbl:String}",
            parameters={"db": db, "tbl": table},
        )
        return {str(row[0]) for row in rows}
    except Exception:
        return set()


def build_from_artifact(artifact: SourceBuildArtifact) -> SchemaBuildResult:
    """Reconstruct a build result from a saved source-build artifact."""
    return SchemaBuildResult(
        source_name=artifact.source_name,
        columns=[],
        create_table_ddl=artifact.create_table_ddl,
        sigma_view_ddl=artifact.sigma_view_ddl,
        view_ddls=dict(artifact.view_ddls),
        validation_errors=list(artifact.validation_errors),
    )


def artifact_from_build(result: SchemaBuildResult, *, version: str) -> SourceBuildArtifact:
    return SourceBuildArtifact(
        source_name=result.source_name,
        version=version,
        built_at=datetime.now(tz=UTC).isoformat(),
        validation_errors=list(result.validation_errors),
        create_table_ddl=result.create_table_ddl or "",
        sigma_view_ddl=result.sigma_view_ddl,
        view_ddls=dict(result.view_ddls or {}),
        column_count=len(result.columns),
    )


def deploy_statements_for_build(
    builder: SchemaBuilderV2,
    source: Source,
    version_id: str,
    result: SchemaBuildResult,
    *,
    db: str,
    ch_client: Any | None = None,
) -> tuple[list[str], bool]:
    """DDL statements to apply for *version_id*, using a prior build result."""
    table = source.table_name
    table_exists = clickhouse_table_exists(ch_client, db, table) if ch_client is not None else False
    statements: list[str] = []
    cfg = builder.build_ddl_config_for_version(source, version_id)

    if not table_exists:
        if result.create_table_ddl:
            statements.append(result.create_table_ddl.strip())
    else:
        existing = _list_table_columns(ch_client, db, table) if ch_client is not None else set()
        ddl_gen = builder._ddl_gen
        for col in result.columns:
            if col.name in existing:
                continue
            add_stmt = ddl_gen.generate_alter_add_column(table, col, cfg)
            statements.append(add_stmt.strip())
            index_stmt = ddl_gen.generate_alter_add_index(table, col, cfg)
            if index_stmt:
                statements.append(index_stmt.strip())

    if result.sigma_view_ddl:
        statements.append(result.sigma_view_ddl.strip())
    for view_ddl in (result.view_ddls or {}).values():
        statements.append(view_ddl.strip())

    return qualify_ddl_statements(statements, db), table_exists


def plan_from_build(
    result: SchemaBuildResult,
    *,
    version: str,
    statements: list[str],
    table_exists: bool,
) -> SourcePlanArtifact:
    errors = list(result.validation_errors)
    ready = not errors and bool(statements)
    return SourcePlanArtifact(
        source_name=result.source_name,
        version=version,
        planned_at=datetime.now(tz=UTC).isoformat(),
        table_exists=table_exists,
        validation_errors=errors,
        statements=statements,
        create_table_ddl=result.create_table_ddl or "",
        view_ddls=dict(result.view_ddls or {}),
        ready=ready,
    )


def execute_ddl_statements(
    client: Any,
    statements: list[str],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Run DDL statements; return (executed, failed)."""
    executed: list[str] = []
    failed: list[tuple[str, str]] = []
    for stmt in statements:
        try:
            client.execute(stmt)
            executed.append(stmt)
            logger.info("Source deploy DDL executed: %s...", stmt[:80])
        except Exception as exc:
            failed.append((stmt, str(exc)))
            logger.error("Source deploy DDL failed: %s — %s", stmt[:80], exc)
    return executed, failed


class SourceDeploymentStore:
    """Filesystem store for source-builds, source-plans, and source-deploys."""

    def __init__(
        self,
        *,
        builds_dir: Path,
        plans_dir: Path,
        deploys_dir: Path,
    ) -> None:
        self.builds_dir = builds_dir
        self.plans_dir = plans_dir
        self.deploys_dir = deploys_dir
        for path in (builds_dir, plans_dir, deploys_dir):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_settings(cls, settings: Any) -> SourceDeploymentStore:
        src = settings.source
        builds = Path(src.builds_dir or Path(src.sources_dir).parent / "source-builds")
        plans = Path(src.plans_dir or Path(src.sources_dir).parent / "source-plans")
        deploys = Path(src.deploys_dir or Path(src.sources_dir).parent / "source-deploys")
        return cls(builds_dir=builds, plans_dir=plans, deploys_dir=deploys)

    def _artifact_path(self, root: Path, source_name: str, version: str) -> Path:
        dest = root / source_name
        dest.mkdir(parents=True, exist_ok=True)
        safe_version = version.replace("/", "_")
        return dest / f"{safe_version}.yaml"

    def save_build(self, artifact: SourceBuildArtifact) -> Path:
        path = self._artifact_path(self.builds_dir, artifact.source_name, artifact.version)
        yaml_dump(artifact.model_dump(mode="json"), path)
        return path

    def load_build(self, source_name: str, version: str) -> SourceBuildArtifact | None:
        path = self._artifact_path(self.builds_dir, source_name, version)
        if not path.is_file():
            return None
        data = yaml_load(path)
        if not isinstance(data, dict):
            return None
        return SourceBuildArtifact.model_validate(data)

    def save_plan(self, artifact: SourcePlanArtifact) -> Path:
        path = self._artifact_path(self.plans_dir, artifact.source_name, artifact.version)
        yaml_dump(artifact.model_dump(mode="json"), path)
        return path

    def load_plan(self, source_name: str, version: str) -> SourcePlanArtifact | None:
        path = self._artifact_path(self.plans_dir, source_name, version)
        if not path.is_file():
            return None
        data = yaml_load(path)
        if not isinstance(data, dict):
            return None
        return SourcePlanArtifact.model_validate(data)

    def save_deploy(self, artifact: SourceDeployArtifact) -> Path:
        path = self._artifact_path(self.deploys_dir, artifact.source_name, artifact.version)
        yaml_dump(artifact.model_dump(mode="json"), path)
        return path


def run_source_build(
    source: Source,
    *,
    version_id: str,
    schemas_base_dir: str | Path | None,
) -> SchemaBuildResult:
    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=schemas_base_dir or None,
    )
    return builder.build_for_source_version(source, source_version=version_id)


def ensure_build_artifact(
    store: SourceDeploymentStore,
    source: Source,
    *,
    version_id: str,
    schemas_base_dir: str | Path | None,
    refresh: bool = False,
) -> tuple[SchemaBuildResult, SourceBuildArtifact]:
    """Load build from source-builds or run build and persist."""
    if not refresh:
        existing = store.load_build(source.source, version_id)
        if existing is not None:
            return build_from_artifact(existing), existing

    result = run_source_build(source, version_id=version_id, schemas_base_dir=schemas_base_dir)
    artifact = artifact_from_build(result, version=version_id)
    store.save_build(artifact)
    return result, artifact
