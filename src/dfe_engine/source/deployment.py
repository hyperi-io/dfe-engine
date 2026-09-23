"""Source ClickHouse plan/deploy — build artifacts, dry-run plans, and deploy execution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field, model_validator
from scalo.logger import logger

from dfe_engine.schema.engine_resolver import EngineResolver
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2, SchemaBuildResult
from dfe_engine.services.schema.json_promotion_service import clickhouse_table_exists
from dfe_engine.source.models import Source
from dfe_engine.source.type_registry import TypeRegistry
from dfe_engine.yaml_utils import yaml_dump, yaml_load

TDoc = TypeVar("TDoc", bound="VersionedSourceArtifactDocument")


class SourceBuildVersionRecord(BaseModel):
    """Build output for one source version.

    The legacy separately-persisted ``sigma_view_ddl`` is folded into
    ``view_ddls["sigma"]`` on read (older on-disk source-builds carry it).
    """

    built_at: str
    validation_errors: list[str] = Field(default_factory=list)
    create_table_ddl: str = ""
    view_ddls: dict[str, str] = Field(default_factory=dict)
    column_count: int = 0

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_sigma_view(cls, data: Any) -> Any:
        if isinstance(data, dict):
            legacy = data.pop("sigma_view_ddl", None)
            if legacy:
                views = dict(data.get("view_ddls") or {})
                views.setdefault("sigma", legacy)
                data["view_ddls"] = views
        return data


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
    ready_reason: str | None = Field(
        default=None,
        description="Why the plan is or is not ready to deploy",
    )


class SchemaDeployResult(BaseModel):
    """Persisted (and API) result of planning/deploying a source version's schema.

    Same object returned by ``POST /api/v1/sources/{name}/deploy`` and nested under
    ``source_deployment`` on version detail.
    """

    source_name: str
    version: str
    dry_run: bool = Field(description="True = plan only; the DDL was NOT applied")
    applied: bool = Field(description="Whether the DDL was executed against ClickHouse")
    create_table: str = Field(description="CREATE TABLE DDL")
    views: dict[str, str] = Field(default_factory=dict, description="View name -> DDL")
    validation_errors: list[str] = Field(default_factory=list)
    statements_applied: int = 0
    topics_ensured: list[str] = Field(
        default_factory=list,
        description="Kafka topics this source needs that now exist (created or already present)",
    )
    topics_failed: list[str] = Field(
        default_factory=list,
        description=(
            "Kafka topics that could not be created. Never fails the deploy - the "
            "schema is live and Kafka may not be in the path at all."
        ),
    )
    apps_synced: list[str] = Field(
        default_factory=list,
        description=(
            "Deploy-repo writes this deploy made so the apps follow the sources: the "
            "receiver and loader routing, and a fetcher instance for a fetcher-based "
            "source (service/instance: action)"
        ),
    )
    apps_sync_error: str | None = Field(
        default=None,
        description=(
            "Why the apps could not be brought into step. Never fails the deploy - "
            "the schema is live; POST /api/v1/sources/reconcile-apps retries it."
        ),
    )
    hyperdx_source_teams: int | None = Field(
        default=None,
        description="How many HyperDX teams now carry a source over this source's table",
    )
    hyperdx_source_error: str | None = Field(
        default=None,
        description=(
            "Why HyperDX was not pointed at the table. Never fails the deploy - "
            "the schema is live and HyperDX may be down or not deployed at all."
        ),
    )
    restart_required: list[str] = Field(
        default_factory=list,
        description=(
            "One command per app whose running process cannot take this deploy's "
            "config change where it stands. Empty where every write was hot, or "
            "where a GitOps controller rolls the pod itself."
        ),
    )


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
    versions: dict[str, SchemaDeployResult] = Field(default_factory=dict)


def _semver_sort_key(version_id: str) -> tuple[int, int, int]:
    parts = str(version_id).split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return (0, 0, 0)
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def previous_deployed_version_ids(
    source: Source,
    deploy_doc: SourceDeployDocument | None,
) -> list[str]:
    """Applied deploy history for this source, excluding the live ``deployed_version``."""
    if deploy_doc is None:
        return []
    live = source.deployed_version
    ids: list[str] = []
    for version_id, record in deploy_doc.versions.items():
        if not record.applied:
            continue
        if live is not None and version_id == live:
            continue
        if version_id not in source.versions:
            continue
        ids.append(version_id)
    return sorted(ids, key=_semver_sort_key)


# Per-version views used by API helpers (flattened from documents).
class SourceBuildArtifact(BaseModel):
    source_name: str
    version: str
    built_at: str
    validation_errors: list[str] = Field(default_factory=list)
    create_table_ddl: str = ""
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
    ready_reason: str | None = None


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
            for index_stmt in ddl_gen.generate_alter_add_indexes(table, col, cfg):
                statements.append(index_stmt.strip())

    for view_ddl in (result.view_ddls or {}).values():
        statements.append(view_ddl.strip())

    return qualify_ddl_statements(statements, db), table_exists


def plan_ready_status(
    *,
    validation_errors: list[str],
    statements: list[str],
    table_exists: bool,
) -> tuple[bool, str]:
    """Return whether a plan can deploy and a human-readable explanation."""
    if validation_errors:
        head = validation_errors[0]
        suffix = f" (+{len(validation_errors) - 1} more)" if len(validation_errors) > 1 else ""
        return False, f"Schema validation failed: {head}{suffix}"
    if not statements:
        if table_exists:
            return (
                False,
                "ClickHouse table already exists and all planned columns are present "
                "(no DDL to apply)",
            )
        return False, "No deploy DDL was generated for this version"
    count = len(statements)
    noun = "statement" if count == 1 else "statements"
    return True, f"{count} DDL {noun} ready to apply"


def plan_from_build(
    result: SchemaBuildResult,
    *,
    version: str,
    statements: list[str],
    table_exists: bool,
) -> SourcePlanArtifact:
    errors = list(result.validation_errors)
    ready, ready_reason = plan_ready_status(
        validation_errors=errors,
        statements=statements,
        table_exists=table_exists,
    )
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
        ready_reason=ready_reason,
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
            logger.info(f"Source deploy DDL executed: {stmt[:80]}...")
        except Exception as exc:
            failed.append((stmt, str(exc)))
            logger.error(f"Source deploy DDL failed: {stmt[:80]} - {exc}")
    return executed, failed


def _build_record_from_artifact(artifact: SourceBuildArtifact) -> SourceBuildVersionRecord:
    return SourceBuildVersionRecord(
        built_at=artifact.built_at,
        validation_errors=list(artifact.validation_errors),
        create_table_ddl=artifact.create_table_ddl,
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
        ready_reason=artifact.ready_reason,
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
        ready_reason=record.ready_reason,
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
                if isinstance(rec, dict) and rec.get("applied"):
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

    def delete_build(self, source_name: str, version: str, *, source: Source | None = None) -> bool:
        """Remove one version entry from source-builds (or delete the file if empty)."""
        path = self._source_file(self.builds_dir, source_name)
        doc = self._read_document(self.builds_dir, source_name, SourceBuildDocument)
        if doc is None or version not in doc.versions:
            return False
        del doc.versions[version]
        if source is not None:
            doc.versions = _prune_versions_to_source(doc.versions, source)
        if not doc.versions:
            if path.is_file():
                path.unlink()
            return True
        if doc.current not in doc.versions:
            doc.current = sorted(doc.versions.keys())[-1]
        yaml_dump(doc.model_dump(mode="json"), path)
        return True

    def delete_source_records(self, source_name: str) -> list[Path]:
        """Remove every build, plan and deploy record for a source; return what went.

        A record left behind outlives the source it describes, and the next source
        to take that name inherits it -- reporting itself built and deployed before
        it ever was.
        """
        removed = []
        for root in (self.builds_dir, self.plans_dir, self.deploys_dir):
            path = self._source_file(root, source_name)
            if path.is_file():
                path.unlink()
                removed.append(path)
        return removed

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

    def save_deploy(self, result: SchemaDeployResult, source: Source) -> Path:
        _assert_version_on_source(source, result.version)
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
        doc.versions[result.version] = result
        if result.applied:
            doc.deployed_version = result.version
        doc.versions = _prune_versions_to_source(doc.versions, source)
        path = self._source_file(self.deploys_dir, source.source)
        yaml_dump(doc.model_dump(mode="json"), path)
        return path

    def load_deploy_document(self, source_name: str) -> SourceDeployDocument | None:
        return self._read_document(
            self.deploys_dir,
            source_name,
            SourceDeployDocument,
            track_deployed_version=True,
        )

    def load_deploy(self, source_name: str, version: str) -> SchemaDeployResult | None:
        doc = self._read_document(
            self.deploys_dir,
            source_name,
            SourceDeployDocument,
            track_deployed_version=True,
        )
        if doc is None or version not in doc.versions:
            return None
        return doc.versions[version]


def run_source_build(
    source: Source,
    *,
    version_id: str,
    schemas_base_dir: str | Path | None,
    resolver: EngineResolver | None = None,
) -> SchemaBuildResult:
    from dfe_engine.schema.derived_registry import derived_reference_root
    from dfe_engine.settings import get_settings

    settings = get_settings()
    ch = settings.clickhouse
    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=schemas_base_dir or None,
        derived_base_dir=derived_reference_root(settings),
        default_engine=ch.default_engine,
        default_ttl_days=ch.default_ttl_days,
        resolver=resolver,
    )
    return builder.build_for_source_version(source, source_version=version_id)


def ensure_build_artifact(
    store: SourceDeploymentStore,
    source: Source,
    *,
    version_id: str,
    schemas_base_dir: str | Path | None,
    refresh: bool = False,
    resolver: EngineResolver | None = None,
) -> tuple[SchemaBuildResult, SourceBuildArtifact]:
    """Load build from source-builds or run build and persist.

    ``resolver`` is the live server's engine resolver when the build is bound for
    that server; the persisted artefact then carries the cluster form of the DDL.
    """
    if not refresh:
        existing = store.load_build(source.source, version_id)
        if existing is not None:
            return build_from_artifact(existing), existing

    result = run_source_build(
        source, version_id=version_id, schemas_base_dir=schemas_base_dir, resolver=resolver
    )
    artifact = artifact_from_build(result, version=version_id)
    store.save_build(artifact, source)
    return result, artifact
