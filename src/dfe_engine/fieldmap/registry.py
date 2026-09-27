"""Field Map Registry -- CRUD management for field map definitions.

Backed by DirectoryConfigStore (YAML directory as SSoT).

Directory layout::

    <field_maps_directory>/
        sigma/
            _default.yaml       # Default Sigma field map
            windows_audit.yaml   # Source-specific override
        ecs/
            _default.yaml
        cim/
            _default.yaml

Each file is ``{source_or__default}.yaml`` inside a ``{standard}/``
subdirectory.

Usage:
    from dfe_engine.fieldmap.registry import FieldMapRegistry

    registry = FieldMapRegistry(field_maps_directory="/etc/dfe/field-maps")
    fm = registry.get_map("sigma", "windows_audit")
    registry.save_map(FieldMap(...))
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.fieldmap.models import DEFAULT_MAP_NAME, FieldMap
from dfe_engine.git_identity import COMMITTER_IDENTITY, commit_file
from dfe_engine.yaml_utils import yaml_dump


class FieldMapError(Exception):
    """Base exception for field map registry errors."""


class FieldMapNotFoundError(FieldMapError):
    """Field map not found in the registry."""


class FieldMapValidationError(FieldMapError):
    """Field map definition failed validation."""


class FieldMapRegistry:
    """Registry for managing field map definitions.

    Backed by DirectoryConfigStore (YAML directory as SSoT).
    Provides CRUD operations, standard-scoped queries, and git-aware writes.
    """

    _instance: FieldMapRegistry | None = None

    def __init__(
        self,
        field_maps_directory: str | Path,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> None:
        self._directory = Path(field_maps_directory)
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
        field_maps_directory: str | Path | None = None,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> FieldMapRegistry:
        """Get singleton registry instance."""
        if cls._instance is None:
            if field_maps_directory is None:
                raise FieldMapError(
                    "field_maps_directory is required on first call to get_instance()"
                )
            cls._instance = FieldMapRegistry(
                field_maps_directory=field_maps_directory,
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
    def _table_name(standard: str, source: str | None = None) -> str:
        """Map standard + source to a DirectoryConfigStore table name."""
        name = source if source else DEFAULT_MAP_NAME
        return f"{standard}/{name}"

    @staticmethod
    def _parse_table_name(table: str) -> tuple[str, str | None]:
        """Parse a table name back into (standard, source).

        Returns source=None if the name part is '_default'.
        """
        parts = table.split("/", 1)
        if len(parts) != 2:
            return table, None
        standard, name = parts
        source = None if name == DEFAULT_MAP_NAME else name
        return standard, source

    # -----------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------

    def get_map(self, standard: str, source: str | None = None) -> FieldMap:
        """Get a field map.

        Args:
            standard: Standard name (e.g. 'sigma').
            source: Source name, or None for the default map.

        Returns:
            FieldMap model.

        Raises:
            FieldMapNotFoundError: Map not found.
        """
        table = self._table_name(standard, source)
        config_data = self._store.get(table)
        if config_data is None:
            desc = f"{standard}/{source}" if source else f"{standard}/_default"
            raise FieldMapNotFoundError(f"Field map not found: '{desc}'")

        return FieldMap.model_validate(config_data)

    def save_map(
        self,
        field_map: FieldMap | dict[str, Any],
        created_by: str | None = None,
        description: str | None = None,
    ) -> FieldMap:
        """Save a field map definition.

        Args:
            field_map: FieldMap model or dict.
            created_by: Username/identity.
            description: Change description.

        Returns:
            Validated FieldMap model.

        Raises:
            FieldMapValidationError: Validation failed.
        """
        if isinstance(field_map, dict):
            try:
                field_map = FieldMap.model_validate(field_map)
            except Exception as e:
                raise FieldMapValidationError(f"Invalid field map definition: {e}") from e

        config_data = field_map.to_yaml_dict()
        table = self._table_name(field_map.standard, field_map.source)

        # Ensure subdirectory exists
        subdir = self._directory / field_map.standard
        subdir.mkdir(parents=True, exist_ok=True)

        # Write YAML
        name = field_map.source if field_map.source else DEFAULT_MAP_NAME
        yaml_path = subdir / f"{name}.yaml"
        yaml_dump(config_data, yaml_path)

        # Git commit if git-aware
        if self._store.is_git:
            commit_msg = description or f"fieldmap: update {table}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            commit_file(self._store, yaml_path, commit_msg, author=created_by)
            if self._store._git_push:
                self._store._git_push_remote()

        # Refresh cache
        self._store._refresh_all()

        logger.info(f"Saved field map '{table}' -> {yaml_path}")
        return field_map

    def delete_map(self, standard: str, source: str | None = None) -> None:
        """Delete a field map.

        Args:
            standard: Standard name.
            source: Source name, or None for the default map.
        """
        table = self._table_name(standard, source)
        name = source if source else DEFAULT_MAP_NAME
        yaml_path = self._directory / standard / f"{name}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Field map file does not exist: {yaml_path}")
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
                    author=COMMITTER_IDENTITY.encode("utf-8"),
                    committer=COMMITTER_IDENTITY.encode("utf-8"),
                    message=f"fieldmap: delete {table}".encode(),
                )
                if self._store._git_push:
                    self._store._git_push_remote()
            except Exception as e:
                logger.error(f"Git delete failed: {e}")
        else:
            yaml_path.unlink()

        with self._store._lock:
            self._store._cache.pop(table, None)

        logger.info(f"Deleted field map '{table}'")

    def list_maps(self, standard: str | None = None) -> list[dict[str, Any]]:
        """List all field maps, optionally filtered by standard.

        Args:
            standard: Optional filter by standard name.

        Returns:
            List of field map metadata dicts.
        """
        results = []
        for table in self._store.list_tables():
            parsed_std, parsed_src = self._parse_table_name(table)
            if standard and parsed_std != standard:
                continue

            config_data = self._store.get(table)
            if config_data is None:
                continue

            try:
                fm = FieldMap.model_validate(config_data)
            except Exception:
                logger.warning(f"Failed to parse field map '{table}', skipping")
                continue

            # Resolve YAML path for modified-time
            name = parsed_src if parsed_src else DEFAULT_MAP_NAME
            yaml_path = self._directory / parsed_std / f"{name}.yaml"
            try:
                stat = yaml_path.stat()
                updated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            except OSError:
                updated_at = None

            results.append(
                {
                    "standard": fm.standard,
                    "source": fm.source,
                    "is_default": fm.is_default,
                    "version": fm.version,
                    "mapping_count": len(fm.mappings),
                    "updated_at": updated_at,
                }
            )

        return results

    def get_maps_for_standard(self, standard: str) -> list[FieldMap]:
        """Get all field maps for a standard.

        Args:
            standard: Standard name (e.g. 'sigma').

        Returns:
            List of FieldMap models.
        """
        maps = []
        for table in self._store.list_tables():
            parsed_std, _ = self._parse_table_name(table)
            if parsed_std != standard:
                continue

            config_data = self._store.get(table)
            if config_data is None:
                continue

            try:
                maps.append(FieldMap.model_validate(config_data))
            except Exception:
                logger.warning(f"Failed to parse field map '{table}', skipping")

        return maps

    def map_exists(self, standard: str, source: str | None = None) -> bool:
        """Check if a field map exists."""
        table = self._table_name(standard, source)
        return self._store.get(table) is not None

    # -----------------------------------------------------------------
    # Seed Defaults
    # -----------------------------------------------------------------

    def seed_defaults(self, overwrite: bool = False) -> int:
        """Seed the field maps directory with the default maps dfe-schemas ships.

        Copies ``registries/field-maps/<standard>/*.yaml``. Non-destructive by
        default: skips files that already exist.

        Args:
            overwrite: If True, overwrite existing maps.

        Returns:
            Number of maps seeded.
        """
        from dfe_engine.schema.schema_loader import SchemaLoadError, resolve_registry_path

        try:
            defaults_dir = resolve_registry_path("field-maps")
        except SchemaLoadError as e:
            logger.warning(f"Failed to locate default field maps: {e}")
            return 0

        count = 0
        for standard_dir in defaults_dir.iterdir():
            if not standard_dir.is_dir() or standard_dir.name.startswith("_"):
                continue

            target_subdir = self._directory / standard_dir.name
            target_subdir.mkdir(parents=True, exist_ok=True)

            for item in standard_dir.iterdir():
                if not item.name.endswith(".yaml"):
                    continue

                target = target_subdir / item.name
                if target.exists() and not overwrite:
                    logger.debug(f"Skipping existing field map: {standard_dir.name}/{item.name}")
                    continue

                content = item.read_text(encoding="utf-8")
                target.write_text(content, encoding="utf-8")
                count += 1
                logger.info(f"Seeded default field map: {standard_dir.name}/{item.name}")

        if count > 0:
            self._store._refresh_all()

        return count

    # -----------------------------------------------------------------
    # Change Callbacks & Git (passthrough)
    # -----------------------------------------------------------------

    def on_change(self, standard: str, source: str | None, callback: Any) -> None:
        """Register a callback for when a field map changes."""
        table = self._table_name(standard, source)
        self._store.on_change(table, callback)

    @property
    def is_git(self) -> bool:
        """Whether the field maps directory is INSIDE a git repository.

        scalo walks up, so a directory under a gitops checkout answers True
        without being a repository root itself.
        """
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
