"""Meta schema Registry — CRUD management for meta schema definitions.

Backed by DirectoryConfigStore (YAML directory as SSoT).

Directory layout::

    <schemas_directory>/
        azure/
            activity_log.yaml   # Azure Activity Log schema
        aws/
            cloudtrail.yaml   # AWS CloudTrail schema
        gcp/
            audit_log.yaml   # GCP Audit Log schema
        *custom_schemas*
            custom_schema.yaml   # Custom schema

Usage:
    from dfe_engine.schema.registry import SchemaRegistry

    registry = SchemaRegistry(schemas_directory="/etc/dfe/schemas")
    schema = registry.get_schema("azure", "activity_log")
    registry.save_schema(Schema(...))
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyperi_pylib.config import DirectoryConfigStore
from hyperi_pylib.logger import logger
from pydantic import ValidationError

from dfe_engine.schema.models import MetaSchema
from dfe_engine.yaml_utils import yaml_dump


class SchemaError(Exception):
    """Base exception for schema registry errors."""


class SchemaNotFoundError(SchemaError):
    """Schema not found in the registry."""


class SchemaValidationError(SchemaError):
    """Schema definition failed validation."""


def _schema_location(schema_path: str) -> tuple[str, str]:
    """Split a registry key into parent path and schema name (YAML stem)."""
    parts = [p for p in schema_path.replace("\\", "/").split("/") if p]
    if not parts:
        raise SchemaValidationError("Invalid empty schema path")
    if len(parts) == 1:
        return "", parts[0]
    return "/".join(parts[:-1]), parts[-1]


def canonical_schema_path(schema_path: str) -> str:
    """Normalize a registry key (forward slashes, no empty segments)."""
    parent, name = _schema_location(schema_path)
    return f"{parent}/{name}" if parent else name


_COMMON_HEADER_PREFIX = "common-header/"


def coerce_common_header_legacy_versions(table: str, config_data: dict[str, Any]) -> dict[str, Any]:
    """Fill missing ``columns`` on version stubs under ``common-header/`` only.

    Legacy common-header YAML kept summary-only ``1.0.0`` entries before columns
    were required on every version.
    """
    key = table.replace("\\", "/")
    if not key.startswith(_COMMON_HEADER_PREFIX):
        return config_data
    versions = config_data.get("versions")
    if not isinstance(versions, dict):
        return config_data
    coerced = dict(config_data)
    coerced_versions: dict[str, Any] = {}
    for ver_id, ver_data in versions.items():
        if isinstance(ver_data, dict) and "columns" not in ver_data:
            coerced_versions[ver_id] = {**ver_data, "columns": []}
        else:
            coerced_versions[ver_id] = ver_data
    coerced["versions"] = coerced_versions
    return coerced


class SchemaRegistry:
    """Registry for managing schema definitions.

    Backed by DirectoryConfigStore (YAML directory as SSoT).
    Provides CRUD operations, standard-scoped queries, and git-aware writes.
    """

    _instance: SchemaRegistry | None = None

    def __init__(
        self,
        schemas_directory: str | Path,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> None:
        # Resolve so YAML paths and git.relative_to(repo_root) agree (relative
        # DFE_SCHEMAS_DIR breaks delete_schema when pylib's repo root is absolute).
        self._directory = Path(schemas_directory).expanduser().resolve(strict=False)
        self._directory.mkdir(parents=True, exist_ok=True)

        self._store = DirectoryConfigStore(
            directory=self._directory,
            refresh_interval=refresh_interval,
            writable=writable,
            git_branch=git_branch,
            git_push=git_push,
        )
        self._store.start()

    @classmethod
    def get_instance(
        cls,
        schemas_directory: str | Path | None = None,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> SchemaRegistry:
        """Get singleton registry instance."""
        if cls._instance is None:
            if schemas_directory is None:
                raise SchemaError("schemas_directory is required on first call to get_instance()")
            cls._instance = SchemaRegistry(
                schemas_directory=schemas_directory,
                writable=writable,
                git_branch=git_branch,
                git_push=git_push,
                refresh_interval=refresh_interval,
            )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        if cls._instance:
            cls._instance.close()
        cls._instance = None

    # -----------------------------------------------------------------
    # Key Convention
    # -----------------------------------------------------------------

    @staticmethod
    def _table_name(path: str) -> str:
        """Map path to a DirectoryConfigStore table name."""
        return f"{path}"

    def _yaml_path(self, table: str) -> Path:
        """Filesystem path for a schema table key (e.g. ``aws/cloudtrail`` → ``.../aws/cloudtrail.yaml``).

        Slashes (and backslashes, normalized to slashes) separate nested directories so keys like
        ``acme/cloudtrail`` and ``contoso/cloudtrail`` map to different files.
        """
        parts = [p for p in table.replace("\\", "/").split("/") if p]
        if not parts:
            raise SchemaValidationError("Invalid empty schema path")
        for seg in parts:
            if seg in (".", ".."):
                raise SchemaValidationError(f"Invalid schema path segment: {seg!r}")
            if "\x00" in seg:
                raise SchemaValidationError("Invalid schema path: null byte in segment")
        if len(parts) == 1:
            candidate = self._directory / f"{parts[0]}.yaml"
        else:
            candidate = self._directory.joinpath(*parts[:-1]) / f"{parts[-1]}.yaml"

        base = self._directory.resolve(strict=False)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(base):
            raise SchemaValidationError("Schema path escapes schemas directory")
        return candidate

    def _parse_meta_schema(self, table: str, config_data: dict[str, Any]) -> MetaSchema:
        return MetaSchema.model_validate(coerce_common_header_legacy_versions(table, config_data))

    # -----------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------

    def get_schema(self, path: str) -> MetaSchema:
        """Get a meta schema.

        Args:
            path: Path to the schema YAML file.

        Returns:
            MetaSchema model.

        Raises:
            SchemaNotFoundError: Schema not found.
        """
        table = self._table_name(path)
        config_data = self._store.get(table)
        if config_data is None:
            desc = f"{path}"
            raise SchemaNotFoundError(f"Schema not found: '{desc}'")

        return self._parse_meta_schema(table, config_data)

    def save_schema(
        self,
        meta_schema: MetaSchema | dict[str, Any],
        created_by: str | None = None,
        description: str | None = None,
    ) -> MetaSchema:
        """Save a meta schema definition.

        Args:
            meta_schema: MetaSchema model or dict.
            created_by: Username/identity.
            description: Change description.

        Returns:
            Validated MetaSchema model.

        Raises:
            SchemaValidationError: Validation failed.
        """
        if isinstance(meta_schema, dict):
            table = str(meta_schema.get("path") or "")
            try:
                meta_schema = self._parse_meta_schema(table, meta_schema)
            except Exception as e:
                raise SchemaValidationError(f"Invalid schema definition: {e}") from e

        if not meta_schema.path:
            raise SchemaValidationError("MetaSchema.path is required when saving")

        config_data = meta_schema.to_yaml_dict()
        table = self._table_name(meta_schema.path)

        yaml_path = self._yaml_path(meta_schema.path)
        yaml_path.parent.mkdir(parents=True, exist_ok=True)

        yaml_dump(config_data, yaml_path)

        # Git commit if git-aware
        if self._store.is_git:
            commit_msg = description or f"schema: update {table}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            self._store._git_commit(yaml_path, commit_msg, author=created_by)
            if self._store._git_push:
                self._store._git_push_remote()

        # Refresh cache
        self._store._refresh_all()

        logger.info(f"Saved schema '{table}' → {yaml_path}")
        return meta_schema

    def notify_schema_file_updated(
        self,
        path: str,
        *,
        description: str | None = None,
        created_by: str | None = None,
    ) -> MetaSchema:
        """Refresh cache (and git-commit) after a direct on-disk schema YAML write."""
        table = self._table_name(path)
        yaml_path = self._yaml_path(path)
        if not yaml_path.exists():
            raise SchemaNotFoundError(f"Schema not found: '{path}'")

        if self._store.is_git:
            commit_msg = description or f"schema: update {table}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            self._store._git_commit(yaml_path, commit_msg, author=created_by)
            if self._store._git_push:
                self._store._git_push_remote()

        self._store._refresh_all()
        logger.info(f"Schema file updated '{table}' → {yaml_path}")
        return self.get_schema(path)

    def delete_schema(self, path: str) -> None:
        """Delete a meta schema.

        Args:
            path: Path to the schema YAML file.
        """
        table = self._table_name(path)
        yaml_path = self._yaml_path(path)

        if not yaml_path.exists():
            logger.warning(f"Schema file does not exist: {yaml_path}")
            return

        if self._store.is_git and self._store._repo is not None:
            try:
                from dulwich import porcelain as git

                repo_root = Path(self._store._repo.path).resolve(strict=False)
                yaml_abs = yaml_path.resolve(strict=False)
                rel_path = str(yaml_abs.relative_to(repo_root))
                # Remove from disk first so dulwich can stage deletion when the
                # index has staged-but-uncommitted changes (e.g. failed commit).
                yaml_abs.unlink(missing_ok=True)
                git.rm(self._store._repo, paths=[rel_path])
                git.commit(
                    self._store._repo,
                    message=f"schema: delete {table}".encode(),
                )
                if self._store._git_push:
                    self._store._git_push_remote()
            except Exception as e:
                logger.error(f"Git delete failed: {e}")
                if yaml_path.exists():
                    yaml_path.unlink(missing_ok=True)
        else:
            yaml_path.unlink(missing_ok=True)

        with self._store._lock:
            self._store._cache.pop(table, None)

        logger.info(f"Deleted schema '{table}'")

    def find_schema_at_location(self, schema_path: str) -> str | None:
        """Return an existing table key that occupies the same path location, if any.

        Uses DirectoryConfigStore table keys only (no YAML parsing). Registry keys
        match on-disk layout; ``MetaSchema.path`` is not persisted in YAML files.
        """
        parent, name = _schema_location(schema_path)
        for table in self._store.list_tables():
            existing_parent, existing_name = _schema_location(table)
            if existing_parent == parent and existing_name == name:
                return table
        return None

    def list_schemas(self, path: str | None = None) -> list[dict[str, Any]]:
        """List all schemas, optionally filtered by path.

        Args:
            path: Optional filter by path.

        Returns:
            List of schema metadata dicts.
        """
        results = []
        for table in self._store.list_tables():
            if path and table != path:
                continue

            config_data = self._store.get(table)
            if config_data is None:
                continue

            try:
                schema = self._parse_meta_schema(table, config_data)
            except ValidationError as e:
                logger.error("Invalid schema '%s' (excluded from list): %s", table, e)
                continue
            except Exception as e:
                logger.error("Failed to load schema '%s' (excluded from list): %s", table, e)
                continue

            # Resolve YAML path for modified-time (use store table key, not list_schemas filter)
            schema_rel = getattr(schema, "path", None) or table
            yaml_path = self._yaml_path(schema_rel)
            try:
                stat = yaml_path.stat()
                updated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            except OSError:
                updated_at = ""

            versions_map = schema.versions or {}
            if schema.current and (cur_ver := versions_map.get(schema.current)):
                n_columns = len(cur_ver.columns)
            elif versions_map:
                n_columns = len(next(iter(versions_map.values())).columns)
            else:
                n_columns = 0

            results.append(
                {
                    "path": schema_rel,
                    "current": schema.current,
                    "versions": list(versions_map.keys()),
                    "column_count": n_columns,
                    "updated_at": updated_at,
                }
            )

        return results

    # -----------------------------------------------------------------
    # Change Callbacks & Git (passthrough)
    # -----------------------------------------------------------------

    def on_change(self, path: str, callback: Any) -> None:
        """Register a callback for when a schema changes."""
        table = self._table_name(path)
        self._store.on_change(table, callback)

    @property
    def is_git(self) -> bool:
        """Whether the schemas directory is a git repository."""
        return self._store.is_git

    @property
    def current_branch(self) -> str | None:
        """Current git branch name."""
        return self._store.current_branch

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def close(self) -> None:
        """Stop background refresh and cleanup."""
        self._store.stop()
