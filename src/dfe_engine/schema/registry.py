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

from dfe_engine.schema.models import MetaSchema
from dfe_engine.yaml_utils import yaml_dump


class SchemaError(Exception):
    """Base exception for schema registry errors."""


class SchemaNotFoundError(SchemaError):
    """Schema not found in the registry."""


class SchemaValidationError(SchemaError):
    """Schema definition failed validation."""


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
        self._directory = Path(schemas_directory)
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
        """Filesystem path for a schema table key (e.g. ``aws/cloudtrail`` → ``.../aws/cloudtrail.yaml``)."""
        parts = [p for p in table.split("/") if p]
        if not parts:
            raise SchemaValidationError("Invalid empty schema path")
        if len(parts) == 1:
            return self._directory / f"{parts[0]}.yaml"
        return self._directory.joinpath(*parts[:-1]) / f"{parts[-1]}.yaml"

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

        return MetaSchema.model_validate(config_data)

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
            try:
                meta_schema = MetaSchema.model_validate(meta_schema)
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

                repo_root = Path(self._store._repo.path)
                rel_path = str(yaml_path.relative_to(repo_root))
                yaml_path.unlink()
                git.rm(self._store._repo, paths=[rel_path])
                git.commit(
                    self._store._repo,
                    message=f"schema: delete {table}".encode(),
                )
                if self._store._git_push:
                    self._store._git_push_remote()
            except Exception as e:
                logger.error(f"Git delete failed: {e}")
        else:
            yaml_path.unlink()

        with self._store._lock:
            self._store._cache.pop(table, None)

        logger.info(f"Deleted schema '{table}'")

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
                schema = MetaSchema.model_validate(config_data)
            except Exception as e:
                logger.warning(f"Failed to parse schema '{table}', skipping: {e}")
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
            top_cols = getattr(schema, "columns", None)
            if top_cols is not None:
                n_columns = len(top_cols)
            elif schema.current and (cur_ver := versions_map.get(schema.current)):
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
                    "description": schema.description or "",
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
