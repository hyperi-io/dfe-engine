"""Source Registry — CRUD management for Source definitions.

Backed by DirectoryConfigStore (YAML directory as SSoT), mirroring the
ServiceConfigRegistry pattern. Each source is a YAML file in the
sources directory.

Directory layout:
    <sources_directory>/
        filebeat.yaml
        syslog.yaml
        crowdstrike_edr.yaml
        ...

Each file is named ``{source_name}.yaml`` and contains the full
Source definition as a YAML document.

Usage:
    from dfe_engine.source.registry import SourceRegistry

    registry = SourceRegistry(sources_directory="/etc/dfe/sources")
    source = registry.get_source("filebeat")
    registry.save_source(Source(...))
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyperi_pylib.config import DirectoryConfigStore
from hyperi_pylib.logger import logger

from dfe_engine.git_identity import COMMITTER_IDENTITY, commit_file
from dfe_engine.source.models import (
    Source,
    SourceWriteRequest,
    apply_source_write_update,
    draft_build_version_to_invalidate,
    source_from_write,
)
from dfe_engine.yaml_utils import yaml_dump


class SourceRegistryError(Exception):
    """Base exception for source registry errors."""


class SourceNotFoundError(SourceRegistryError):
    """Source not found in the registry."""


class SourceValidationError(SourceRegistryError):
    """Source definition failed validation."""


class SourceMatchConflictError(SourceValidationError):
    """Two enabled sources share the same receiver match (field + value)."""

    def __init__(
        self,
        *,
        source: str,
        conflicting_source: str,
        field: str,
        value: str,
    ) -> None:
        self.source = source
        self.conflicting_source = conflicting_source
        self.field = field
        self.value = value
        super().__init__(
            f"Cannot save source {source!r}: the match field {field!r} with value {value!r} "
            f"is already used by enabled source {conflicting_source!r}. "
            f"Use a different field/value pair, or disable {conflicting_source!r} first."
        )


class SourceRegistry:
    """Registry for managing Source definitions.

    Backed by DirectoryConfigStore (YAML directory as SSoT).
    Provides CRUD operations, validation (unique _source, no match
    conflicts), change callbacks, and git-aware writes.
    """

    _instance: SourceRegistry | None = None

    def __init__(
        self,
        sources_directory: str | Path,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> None:
        """Initialize the registry.

        Args:
            sources_directory: Path to the YAML sources directory.
            writable: Whether writes are allowed. None = auto-detect.
            git_branch: Git branch for writes. None = current branch.
            git_push: Auto-push after git commits.
            refresh_interval: Seconds between background cache refresh polls.
        """
        self._sources_directory = Path(sources_directory)
        self._sources_directory.mkdir(parents=True, exist_ok=True)

        self._store = DirectoryConfigStore(
            directory=self._sources_directory,
            refresh_interval=refresh_interval,
            writable=writable,
            git_branch=git_branch,
            git_push=git_push,
        )
        self._store.start()

    @classmethod
    def get_instance(
        cls,
        sources_directory: str | Path | None = None,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
    ) -> SourceRegistry:
        """Get singleton registry instance."""
        if cls._instance is None:
            if sources_directory is None:
                raise SourceRegistryError(
                    "sources_directory is required on first call to get_instance()"
                )
            cls._instance = SourceRegistry(
                sources_directory=sources_directory,
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
    # CRUD Operations
    # -----------------------------------------------------------------

    def get_source(self, source_name: str) -> Source:
        """Get a source definition.

        Args:
            source_name: The _source label (e.g. 'filebeat').

        Returns:
            Source model.

        Raises:
            SourceNotFoundError: Source not found.
        """
        config_data = self._store.get(source_name)
        if config_data is None:
            raise SourceNotFoundError(f"Source not found: {source_name!r}")

        return Source.model_validate(config_data)

    def save_source(
        self,
        source: Source | dict[str, Any],
        created_by: str | None = None,
        description: str | None = None,
    ) -> Source:
        """Save a source definition to the YAML directory.

        Validates the source (Pydantic + uniqueness + match conflicts)
        before writing. If the directory is a git repo, changes are
        auto-committed.

        Args:
            source: Source model or dict.
            created_by: Username/identity of who made the change.
            description: Description of the change.

        Returns:
            Validated Source model.

        Raises:
            SourceValidationError: Validation failed.
        """
        # Normalize to Source model
        if isinstance(source, dict):
            try:
                source = Source.model_validate(source)
            except Exception as e:
                raise SourceValidationError(f"Invalid source definition: {e}") from e

        # Validate uniqueness and match conflicts
        self._validate_save(source)

        # Serialize and write
        config_data = source.to_yaml_dict()
        yaml_path = self._sources_directory / f"{source.source}.yaml"
        yaml_dump(config_data, yaml_path)

        # Git commit if git-aware
        if self._store.is_git:
            commit_msg = description or f"source: update {source.source}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            commit_file(self._store, yaml_path, commit_msg, author=created_by)
            if self._store._git_push:
                self._store._git_push_remote()

        # Force cache refresh
        self._store._refresh_all()

        logger.info(f"Saved source {source.source!r} → {yaml_path}")
        return source

    def create_source_from_write(
        self,
        write: SourceWriteRequest | dict[str, Any],
        *,
        created_by: str | None = None,
        description: str | None = None,
    ) -> Source:
        """Create a source from a flat write body (initial version ``1.0.0``)."""
        if isinstance(write, dict):
            try:
                write = SourceWriteRequest.model_validate(write)
            except Exception as e:
                raise SourceValidationError(f"Invalid source definition: {e}") from e

        if not write.source:
            raise SourceValidationError("'source' field is required")

        source = source_from_write(write, source_name=write.source)
        return self.save_source(source, created_by=created_by, description=description)

    def update_source_from_write(
        self,
        source_name: str,
        write: SourceWriteRequest | dict[str, Any],
        *,
        created_by: str | None = None,
        description: str | None = None,
        deployment_store: Any | None = None,
    ) -> Source:
        """Update a source; version bump only when the deployed version is edited."""
        if isinstance(write, dict):
            try:
                write = SourceWriteRequest.model_validate(write)
            except Exception as e:
                raise SourceValidationError(f"Invalid source definition: {e}") from e

        existing = self.get_source(source_name)
        try:
            updated = apply_source_write_update(existing, write)
        except ValueError as e:
            raise SourceValidationError(str(e)) from e

        invalidate_version = draft_build_version_to_invalidate(existing, updated)
        if invalidate_version is not None:
            store = deployment_store
            if store is None:
                try:
                    from dfe_engine.settings import get_settings
                    from dfe_engine.source.deployment import SourceDeploymentStore

                    store = SourceDeploymentStore.from_settings(get_settings())
                except Exception:
                    store = None
            if store is not None and store.load_build(source_name, invalidate_version) is not None:
                store.delete_build(source_name, invalidate_version, source=updated)

        return self.save_source(updated, created_by=created_by, description=description)

    def set_deployed_version(
        self,
        source_name: str,
        version_id: str,
        *,
        created_by: str | None = None,
        description: str | None = None,
    ) -> Source:
        """Record which source version is deployed to ClickHouse runtime."""
        source = self.get_source(source_name)
        if version_id not in source.versions:
            raise SourceValidationError(
                f"Version '{version_id}' is not defined for source '{source_name}'"
            )
        updated = source.model_copy(update={"deployed_version": version_id})
        msg = description or f"source: deploy {source_name} version {version_id}"
        return self.save_source(updated, created_by=created_by, description=msg)

    def delete_source(self, source_name: str) -> None:
        """Delete a source definition.

        Removes the YAML file and commits the deletion if git-aware.

        Args:
            source_name: The _source label.
        """
        yaml_path = self._sources_directory / f"{source_name}.yaml"

        if not yaml_path.exists():
            logger.warning(f"Source file does not exist: {yaml_path}")
            return

        # Git rm + commit if git-aware
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
                    message=f"source: delete {source_name}".encode(),
                )
                if self._store._git_push:
                    self._store._git_push_remote()
            except Exception as e:
                logger.error(f"Git delete failed: {e}")
        else:
            yaml_path.unlink()

        # Remove from cache
        with self._store._lock:
            self._store._cache.pop(source_name, None)

        logger.info(f"Deleted source {source_name!r}")

    def list_sources(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """List all source definitions.

        Args:
            enabled_only: If True, only return enabled sources.

        Returns:
            List of source metadata dicts (source, display_name, enabled, updated_at).
        """
        results = []
        for table in self._store.list_tables():
            config_data = self._store.get(table)
            if config_data is None:
                continue

            try:
                source = Source.model_validate(config_data)
            except Exception:
                logger.warning(f"Failed to parse source {table!r}, skipping")
                continue

            if enabled_only and not source.enabled:
                continue

            yaml_path = self._sources_directory / f"{table}.yaml"
            try:
                stat = yaml_path.stat()
                updated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            except OSError:
                updated_at = None

            results.append(
                {
                    "source": source.source,
                    "display_name": source.display_name,
                    "description": source.description,
                    "enabled": source.enabled,
                    "current": source.current,
                    "deployed_version": source.deployed_version,
                    "versions": sorted(source.versions.keys()),
                    "header_type": source.header.type if source.header else None,
                    "has_transform": source.transform is not None,
                    "has_fetcher": source.fetcher is not None,
                    "mapping_standards": list(source.mapping_standards),
                    "updated_at": updated_at or "",
                }
            )

        return results

    def get_all_sources(self, enabled_only: bool = False) -> list[Source]:
        """Load all source definitions as Source models.

        Args:
            enabled_only: If True, only return enabled sources.

        Returns:
            List of Source models.
        """
        sources = []
        for table in self._store.list_tables():
            config_data = self._store.get(table)
            if config_data is None:
                continue
            try:
                source = Source.model_validate(config_data)
                if enabled_only and not source.enabled:
                    continue
                sources.append(source)
            except Exception:
                logger.warning(f"Failed to parse source {table!r}, skipping")

        return sources

    def source_exists(self, source_name: str) -> bool:
        """Check if a source exists in the registry."""
        return self._store.get(source_name) is not None

    # -----------------------------------------------------------------
    # Match Table
    # -----------------------------------------------------------------

    def compile_match_table(self) -> list[dict[str, str]]:
        """Compile the receiver match table from all enabled sources.

        Returns:
            List of match rules: [{field, value, source}]
        """
        rules = []
        for source in self.get_all_sources(enabled_only=True):
            if source.match:
                rules.append(
                    {
                        "field": source.match.field,
                        "value": source.match.value,
                        "source": source.source,
                    }
                )
        return rules

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    def _validate_save(self, source: Source) -> None:
        """Validate before saving: unique source, no match conflicts."""
        try:
            candidate_match = source.versions[source.current].match
        except KeyError:
            candidate_match = None

        for table in self._store.list_tables():
            if table == source.source:
                continue  # Same source (update)

            config_data = self._store.get(table)
            if config_data is None:
                continue

            try:
                existing = Source.model_validate(config_data)
            except Exception:
                continue

            # Check match conflicts: same field+value on different sources
            if (
                candidate_match
                and existing.match
                and existing.enabled
                and source.enabled
                and candidate_match.field == existing.match.field
                and candidate_match.value == existing.match.value
            ):
                raise SourceMatchConflictError(
                    source=source.source,
                    conflicting_source=existing.source,
                    field=candidate_match.field,
                    value=candidate_match.value,
                )

    # -----------------------------------------------------------------
    # Change Callbacks
    # -----------------------------------------------------------------

    def on_change(self, source_name: str, callback: Any) -> None:
        """Register a callback for when a source config changes.

        Args:
            source_name: The _source label.
            callback: Function called with (table_name, data) on change.
        """
        self._store.on_change(source_name, callback)

    # -----------------------------------------------------------------
    # Git Operations (passthrough)
    # -----------------------------------------------------------------

    @property
    def is_git(self) -> bool:
        """Whether the sources directory is a git repository."""
        return self._store.is_git

    @property
    def current_branch(self) -> str | None:
        """Current git branch name."""
        return self._store.current_branch

    def list_branches(self) -> list[str]:
        """List all git branches."""
        return self._store.list_branches()

    def switch_branch(self, branch: str, create: bool = False) -> None:
        """Switch to a git branch. Refreshes cache after switch."""
        self._store.switch_branch(branch, create=create)

    # -----------------------------------------------------------------
    # Built-in Sources
    # -----------------------------------------------------------------

    def seed_builtin_sources(self, overwrite: bool = False) -> int:
        """Seed the sources directory with built-in source definitions.

        Copies built-in YAML files from package resources. Non-destructive
        by default — skips files that already exist.

        Args:
            overwrite: If True, overwrite existing source definitions.

        Returns:
            Number of sources seeded.
        """
        import importlib.resources as resources

        try:
            builtins_dir = resources.files("dfe_engine.source") / "builtin_sources"
        except Exception as e:
            logger.warning(f"Failed to locate built-in sources: {e}")
            return 0

        count = 0
        for resource in builtins_dir.iterdir():
            if not resource.name.endswith(".yaml"):
                continue

            source_name = resource.name.removesuffix(".yaml")
            if not overwrite and self.source_exists(source_name):
                logger.debug(f"Built-in source {source_name!r} already exists, skipping")
                continue

            try:
                content = resource.read_text()
                dest = self._sources_directory / resource.name
                dest.write_text(content)
                count += 1
                logger.info(f"Seeded built-in source: {source_name}")
            except Exception as e:
                logger.warning(f"Failed to seed source {source_name!r}: {e}")

        if count > 0:
            self._store._refresh_all()

        return count

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def close(self) -> None:
        """Stop background refresh and cleanup."""
        self._store.stop()
