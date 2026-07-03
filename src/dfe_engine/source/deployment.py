"""Source ClickHouse plan/deploy — build artifacts, dry-run plans, and deploy execution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from hyperi_pylib.logger import logger
from pydantic import BaseModel, Field

from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2, SchemaBuildResult
from dfe_engine.services.schema.json_promotion_service import clickhouse_table_exists
from dfe_engine.source.models import Source
from dfe_engine.source.type_registry import TypeRegistry
from dfe_engine.yaml_utils import yaml_dump, yaml_load

TDoc = TypeVar("TDoc", bound="VersionedSourceArtifactDocument")


class SourceBuildVersionRecord(BaseModel):
    """Build output for one source version."""

    built_at: str
    validation_errors: list[str] = Field(default_factory=list)
    create_table_ddl: str = ""
    sigma_view_ddl: str | None = None
    view_ddls: dict[str, str] = Field(default_factory=dict)
    column_count: int = 0


class SourcePlanVersionRecord(BaseModel):
    """Dry-run deploy plan for one source version."""

    planned_at: str
    table_exists: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    statements: list[str] = Field(default_factory=list)
    create_table_ddl: str = ""
    view_ddls: dict[str, str] = Field(default_factory=dict)
    ready: bool = Field(
        description="True when there are no validation errors and deploy statements are present"
    )


class SourceDeployVersionRecord(BaseModel):
    """Deploy run result for one source version."""

    deployed_at: str
    success: bool
    ddl_executed: list[str] = Field(default_factory=list)
    ddl_failed: list[dict[str, str]] = Field(default_factory=list)


class VersionedSourceArtifactDocument(BaseModel):
    """One YAML file per source (mirrors ``sources/{name}.yaml`` layout)."""

    source: str
    current: str = Field(description="Most recently written version id for this artifact type")
    versions: dict[str, Any] = Field(default_factory=dict)


class SourceBuildDocument(VersionedSourceArtifactDocument):
    versions: dict[str, SourceBuildVersionRecord] = Field(default_factory=dict)


class SourcePlanDocument(VersionedSourceArtifactDocument):
    versions: dict[str, SourcePlanVersionRecord] = Field(default_factory=dict)


class SourceDeployDocument(BaseModel):
    """Deploy history file — includes ``deployed_version`` like ``Source``."""

    source: str
    deployed_version: str | None = Field(
        default=None,
        description="Last successfully deployed version (at most one live deploy)",
    )
    versions: dict[str, SourceDeployVersionRecord] = Field(default_factory=dict)


# Per-version views used by API helpers (flattened from documents).
class SourceBuildArtifact(BaseModel):
    source_name: str
    version: str
    built_at: str
    validation_errors: list[str] = Field(default_factory=list)
    create_table_ddl: str = ""
    sigma_view_ddl: str | None = None
    view_ddls: dict[str, str] = Field(default_factory=dict)
    column_count: int = 0


class SourcePlanArtifact(BaseModel):
    source_name: str
    version: str
    planned_at: str
    table_exists: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    statements: list[str] = Field(default_factory=list)
    create_table_ddl: str = ""
    view_ddls: dict[str, str] = Field(default_factory=dict)
    ready: bool = False


class SourceDeployArtifact(BaseModel):
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


def _assert_version_on_source(source: Source, version_id: str) -> None:
    if version_id not in source.versions:
        raise ValueError(
            f"Version '{version_id}' is not defined on source '{source.source}' "
            f"(defined: {sorted(source.versions.keys())})"
        )


def _prune_versions_to_source(
    versions: dict[str, Any],
    source: Source,
) -> dict[str, Any]:
    allowed = set(source.versions.keys())
    return {vid: rec for vid, rec in versions.items() if vid in allowed}


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


def _build_record_from_artifact(artifact: SourceBuildArtifact) -> SourceBuildVersionRecord:
    return SourceBuildVersionRecord(
        built_at=artifact.built_at,
        validation_errors=list(artifact.validation_errors),
        create_table_ddl=artifact.create_table_ddl,
        sigma_view_ddl=artifact.sigma_view_ddl,
        view_ddls=dict(artifact.view_ddls),
        column_count=artifact.column_count,
    )


def _build_artifact_from_record(
    source_name: str,
    version: str,
    record: SourceBuildVersionRecord,
) -> SourceBuildArtifact:
    return SourceBuildArtifact(
        source_name=source_name,
        version=version,
        built_at=record.built_at,
        validation_errors=list(record.validation_errors),
        create_table_ddl=record.create_table_ddl,
        sigma_view_ddl=record.sigma_view_ddl,
        view_ddls=dict(record.view_ddls),
        column_count=record.column_count,
    )


def _plan_record_from_artifact(artifact: SourcePlanArtifact) -> SourcePlanVersionRecord:
    return SourcePlanVersionRecord(
        planned_at=artifact.planned_at,
        table_exists=artifact.table_exists,
        validation_errors=list(artifact.validation_errors),
        statements=list(artifact.statements),
        create_table_ddl=artifact.create_table_ddl,
        view_ddls=dict(artifact.view_ddls),
        ready=artifact.ready,
    )


def _plan_artifact_from_record(
    source_name: str,
    version: str,
    record: SourcePlanVersionRecord,
) -> SourcePlanArtifact:
    return SourcePlanArtifact(
        source_name=source_name,
        version=version,
        planned_at=record.planned_at,
        table_exists=record.table_exists,
        validation_errors=list(record.validation_errors),
        statements=list(record.statements),
        create_table_ddl=record.create_table_ddl,
        view_ddls=dict(record.view_ddls),
        ready=record.ready,
    )


def _deploy_record_from_artifact(artifact: SourceDeployArtifact) -> SourceDeployVersionRecord:
    return SourceDeployVersionRecord(
        deployed_at=artifact.deployed_at,
        success=artifact.success,
        ddl_executed=list(artifact.ddl_executed),
        ddl_failed=list(artifact.ddl_failed),
    )


def _deploy_artifact_from_record(
    source_name: str,
    version: str,
    record: SourceDeployVersionRecord,
    *,
    deployed_version: str | None,
) -> SourceDeployArtifact:
    return SourceDeployArtifact(
        source_name=source_name,
        version=version,
        deployed_at=record.deployed_at,
        success=record.success,
        deployed_version=deployed_version if record.success else None,
        ddl_executed=list(record.ddl_executed),
        ddl_failed=list(record.ddl_failed),
    )


class SourceDeploymentStore:
    """Filesystem store: ``{dir}/{source}.yaml`` with a ``versions`` map."""

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

    @staticmethod
    def _source_file(root: Path, source_name: str) -> Path:
        return root / f"{source_name}.yaml"

    @staticmethod
    def _load_yaml_dict(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        data = yaml_load(path)
        return data if isinstance(data, dict) else None

    def _migrate_legacy_version_files(
        self,
        root: Path,
        source_name: str,
        *,
        version_key: str = "version",
        track_deployed_version: bool = False,
    ) -> dict[str, Any] | None:
        """Merge ``{root}/{source}/{version}.yaml`` into a single-document dict."""
        legacy_dir = root / source_name
        if not legacy_dir.is_dir():
            return None
        merged_versions: dict[str, Any] = {}
        for path in sorted(legacy_dir.glob("*.yaml")):
            data = self._load_yaml_dict(path)
            if not data:
                continue
            vid = str(data.pop(version_key, path.stem))
            data.pop("source_name", None)
            data.pop("source", None)
            merged_versions[vid] = data
        if not merged_versions:
            return None
        latest = max(merged_versions.keys())
        out: dict[str, Any] = {
            "source": source_name,
            "current": latest,
            "versions": merged_versions,
        }
        if track_deployed_version:
            deployed = None
            for vid, rec in merged_versions.items():
                if isinstance(rec, dict) and rec.get("success"):
                    deployed = vid
            out["deployed_version"] = deployed
        return out

    def _read_document(
        self,
        root: Path,
        source_name: str,
        doc_class: type[TDoc],
        *,
        legacy_version_key: str = "version",
        track_deployed_version: bool = False,
    ) -> TDoc | None:
        path = self._source_file(root, source_name)
        data = self._load_yaml_dict(path)
        if data is None:
            data = self._migrate_legacy_version_files(
                root,
                source_name,
                version_key=legacy_version_key,
                track_deployed_version=track_deployed_version,
            )
            if data is not None:
                yaml_dump(data, path)
        if data is None:
            return None
        return doc_class.model_validate(data)

    def save_build(self, artifact: SourceBuildArtifact, source: Source) -> Path:
        _assert_version_on_source(source, artifact.version)
        doc = self._read_document(
            self.builds_dir, source.source, SourceBuildDocument
        ) or SourceBuildDocument(
            source=source.source,
            current=artifact.version,
            versions={},
        )
        doc.versions[artifact.version] = _build_record_from_artifact(artifact)
        doc.current = artifact.version
        doc.versions = _prune_versions_to_source(doc.versions, source)
        path = self._source_file(self.builds_dir, source.source)
        yaml_dump(doc.model_dump(mode="json"), path)
        return path

    def load_build(self, source_name: str, version: str) -> SourceBuildArtifact | None:
        doc = self._read_document(self.builds_dir, source_name, SourceBuildDocument)
        if doc is None or version not in doc.versions:
            return None
        return _build_artifact_from_record(source_name, version, doc.versions[version])

    def save_plan(self, artifact: SourcePlanArtifact, source: Source) -> Path:
        _assert_version_on_source(source, artifact.version)
        doc = self._read_document(
            self.plans_dir, source.source, SourcePlanDocument
        ) or SourcePlanDocument(
            source=source.source,
            current=artifact.version,
            versions={},
        )
        doc.versions[artifact.version] = _plan_record_from_artifact(artifact)
        doc.current = artifact.version
        doc.versions = _prune_versions_to_source(doc.versions, source)
        path = self._source_file(self.plans_dir, source.source)
        yaml_dump(doc.model_dump(mode="json"), path)
        return path

    def load_plan(self, source_name: str, version: str) -> SourcePlanArtifact | None:
        doc = self._read_document(self.plans_dir, source_name, SourcePlanDocument)
        if doc is None or version not in doc.versions:
            return None
        return _plan_artifact_from_record(source_name, version, doc.versions[version])

    def save_deploy(self, artifact: SourceDeployArtifact, source: Source) -> Path:
        _assert_version_on_source(source, artifact.version)
        doc = self._read_document(
            self.deploys_dir,
            source.source,
            SourceDeployDocument,
            track_deployed_version=True,
        ) or SourceDeployDocument(
            source=source.source,
            deployed_version=None,
            versions={},
        )
        doc.versions[artifact.version] = _deploy_record_from_artifact(artifact)
        if artifact.success:
            doc.deployed_version = artifact.version
        doc.versions = _prune_versions_to_source(doc.versions, source)
        path = self._source_file(self.deploys_dir, source.source)
        yaml_dump(doc.model_dump(mode="json"), path)
        return path

    def load_deploy(self, source_name: str, version: str) -> SourceDeployArtifact | None:
        doc = self._read_document(
            self.deploys_dir,
            source_name,
            SourceDeployDocument,
            track_deployed_version=True,
        )
        if doc is None or version not in doc.versions:
            return None
        return _deploy_artifact_from_record(
            source_name,
            version,
            doc.versions[version],
            deployed_version=doc.deployed_version,
        )


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
    store.save_build(artifact, source)
    return result, artifact
