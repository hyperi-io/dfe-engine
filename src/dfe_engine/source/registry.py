"""Source Registry — CRUD management for Source definitions.

Two interchangeable backends behind one registry surface:

- **gitcrud** (preferred): the all-in-one source YAML IS the gitcrud doc in the
  deploy repo's ``config/sources/`` (ResourceClass ``sources``). Every mutation
  is one git commit; the stored doc carries the universal gitcrud ``metadata``
  block. Pass ``crud=GitCrud(...)`` to select it.
- **DirectoryConfigStore** (standalone): a plain YAML directory as SSoT,
  mirroring the ServiceConfigRegistry pattern. Used when no gitops deploy repo
  is configured.

Each file is named ``{source_name}.yaml`` and contains the full
Source definition as a YAML document (API payload = exchange file = stored doc).

Usage:
    from dfe_engine.source.registry import SourceRegistry

    registry = SourceRegistry(sources_directory="/etc/dfe/sources")
    source = registry.get_source("filebeat")
    registry.save_source(Source(...))
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.git_identity import COMMITTER_IDENTITY, commit_file
from dfe_engine.source.models import (
    Source,
    SourceWriteRequest,
    apply_source_write_update,
    draft_build_version_to_invalidate,
    source_from_write,
)
from dfe_engine.yaml_utils import yaml_dump

if TYPE_CHECKING:
    from dfe_engine.gitcrud.engine import GitCrud

_SOURCES_CLASS = "sources"


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

    Backed by the gitcrud engine (deploy-repo ``config/sources/``) when a
    ``crud`` is provided, else by DirectoryConfigStore (YAML directory as SSoT).
    Provides CRUD operations, validation (unique _source, no match
    conflicts), change callbacks, and git-aware writes.
    """

    _instance: SourceRegistry | None = None

    def __init__(
        self,
        sources_directory: str | Path | None = None,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
        *,
        crud: GitCrud | None = None,
    ) -> None:
        """Initialize the registry.

        Args:
            sources_directory: Path to the YAML sources directory
                (DirectoryConfigStore backend; ignored when ``crud`` is given).
            writable: Whether writes are allowed. None = auto-detect.
            git_branch: Git branch for writes. None = current branch.
            git_push: Auto-push after git commits.
            refresh_interval: Seconds between background cache refresh polls.
            crud: Governed Ops engine over the deploy repo. When provided the
                ``sources`` resource class (config/sources/) is the SSoT and
                every mutation is one git commit.
        """
        self._crud = crud
        self._store: DirectoryConfigStore | None = None

        if crud is not None:
            cls = crud.resource_class(_SOURCES_CLASS)
            self._sources_directory = crud.repo_path / cls.directory
            return

        if sources_directory is None:
            raise SourceRegistryError("sources_directory is required without a gitcrud backend")
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
    # Backend primitives (gitcrud vs DirectoryConfigStore)
    # -----------------------------------------------------------------

    def _names(self) -> list[str]:
        """All stored source names."""
        if self._crud is not None:
            return self._crud.list(_SOURCES_CLASS)
        return list(self._store.list_tables())

    def _get_raw(self, source_name: str) -> dict[str, Any] | None:
        """Raw stored doc for a source, or None when absent."""
        if self._crud is not None:
            from dfe_engine.gitcrud.engine import ResourceNotFoundError

            try:
                return self._crud.get(_SOURCES_CLASS, source_name)
            except ResourceNotFoundError:
                return None
        return self._store.get(source_name)

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
        config_data = self._get_raw(source_name)
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

        if self._crud is not None:
            # gitcrud backend: the doc carries the universal metadata block and
            # every mutation is ONE commit in the deploy repo.
            from dfe_engine.gitcrud.metadata import ResourceMetadata, with_metadata

            doc = with_metadata(
                config_data,
                ResourceMetadata(
                    description=source.description or "",
                    display_name=source.display_name,
                ),
            )
            commit_msg = description or f"source: update {source.source}"
            if created_by:
                commit_msg = f"{commit_msg} (by {created_by})"
            self._crud.put(
                _SOURCES_CLASS,
                source.source,
                doc,
                actor=created_by or "engine",
                message=commit_msg,
            )
            logger.info(f"Saved source {source.source!r} → gitcrud config/sources")
            return source

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
        if self._crud is not None:
            from dfe_engine.gitcrud.engine import ResourceNotFoundError

            try:
                self._crud.delete(
                    _SOURCES_CLASS,
                    source_name,
                    actor="engine",
                    message=f"source: delete {source_name}",
                )
            except ResourceNotFoundError:
                logger.warning(f"Source does not exist: {source_name!r}")
                return
            logger.info(f"Deleted source {source_name!r}")
            return

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
            enabled_only: If True, only return active sources.

        Returns:
            List of source metadata dicts (source, display_name, state, updated_at).
        """
        results = []
        for table in self._names():
            config_data = self._get_raw(table)
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
                    "state": source.state,
                    "enabled": source.enabled,
                    "current": source.current,
                    "deployed_version": source.deployed_version,
                    "versions": sorted(source.versions.keys()),
                    "header_type": source.header.type if source.header else None,
                    "has_transform": source.transform is not None,
                    "has_fetcher": source.fetcher is not None,
                    "views": [v.standard for v in source.views],
                    "updated_at": updated_at or "",
                }
            )

        return results

    def get_all_sources(
        self,
        enabled_only: bool = False,
        *,
        states: Collection[str] | None = None,
    ) -> list[Source]:
        """Load all source definitions as Source models.

        Args:
            enabled_only: If True, only return ACTIVE sources (compat filter).
            states: Explicit tri-state filter (e.g. {"active", "dormant"} for
                the schema-pre-positioning DDL compile). Wins over enabled_only.

        Returns:
            List of Source models.
        """
        sources = []
        for table in self._names():
            config_data = self._get_raw(table)
            if config_data is None:
                continue
            try:
                source = Source.model_validate(config_data)
                if states is not None:
                    if source.state not in states:
                        continue
                elif enabled_only and not source.enabled:
                    continue
                sources.append(source)
            except Exception:
                logger.warning(f"Failed to parse source {table!r}, skipping")

        return sources

    def source_exists(self, source_name: str) -> bool:
        """Check if a source exists in the registry."""
        return self._get_raw(source_name) is not None

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
                        "operator": source.match.operator,
                        "value": source.match.value,
                        "source": source.source,
                    }
                )
        return rules

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    def _validate_save(self, source: Source) -> None:
        """Validate before saving: unique source, receiver-evaluable match, no conflicts."""
        try:
            candidate_match = source.versions[source.current].match
        except KeyError:
            candidate_match = None

        # A source with a match is receiver-routed: its operator must map onto
        # the receiver's hot-path modes (equals -> key_value_set, exists ->
        # key_present). The other four operators are a DOCUMENTED receiver gap.
        if candidate_match is not None and candidate_match.operator not in ("equals", "exists"):
            from dfe_engine.services.source_routing import UnsupportedMatchOperatorError

            raise SourceValidationError(
                str(UnsupportedMatchOperatorError(source.source, candidate_match.operator))
            )

        for table in self._names():
            if table == source.source:
                continue  # Same source (update)

            config_data = self._get_raw(table)
            if config_data is None:
                continue

            try:
                existing = Source.model_validate(config_data)
            except Exception:
                continue

            # Check match conflicts: same field+value on different sources.
            # Dormant counts as conflicting (its schema is pre-positioned and it
            # may activate later); only disabled sources release their match.
            if (
                candidate_match
                and existing.match
                and existing.state != "disabled"
                and source.state != "disabled"
                and candidate_match.field == existing.match.field
                and candidate_match.operator == existing.match.operator
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

        No-op on the gitcrud backend: the engine is the only writer there, so
        there is no background poller to observe out-of-band edits.
        """
        if self._crud is not None:
            logger.debug("on_change is a no-op on the gitcrud sources backend")
            return
        self._store.on_change(source_name, callback)

    # -----------------------------------------------------------------
    # Git Operations (passthrough)
    # -----------------------------------------------------------------

    @property
    def is_git(self) -> bool:
        """Whether the sources backend is git-native."""
        if self._crud is not None:
            return True
        return self._store.is_git

    @property
    def current_branch(self) -> str | None:
        """Current git branch name."""
        if self._crud is not None:
            return self._crud.repo.branch
        return self._store.current_branch

    def list_branches(self) -> list[str]:
        """List all git branches."""
        if self._crud is not None:
            raise SourceRegistryError(
                "branch operations are owned by the gitcrud routing layer on this backend"
            )
        return self._store.list_branches()

    def switch_branch(self, branch: str, create: bool = False) -> None:
        """Switch to a git branch. Refreshes cache after switch."""
        if self._crud is not None:
            raise SourceRegistryError(
                "branch operations are owned by the gitcrud routing layer on this backend"
            )
        self._store.switch_branch(branch, create=create)

    # -----------------------------------------------------------------
    # Built-in Sources
    # -----------------------------------------------------------------

    def seed_builtin_sources(self, overwrite: bool = False) -> int:
        """Seed the sources directory with built-in source definitions.

        Copy-on-adopt: built-ins ship in-engine and are copied into the
        deployment's sources store. Non-destructive by default — skips
        sources that already exist. On the gitcrud backend all seeds land
        in ONE commit.

        Args:
            overwrite: If True, overwrite existing source definitions.

        Returns:
            Number of sources seeded.
        """
        import importlib.resources as resources

        from dfe_engine.yaml_utils import yaml_load_string

        try:
            builtins_dir = resources.files("dfe_engine.source") / "builtin_sources"
        except Exception as e:
            logger.warning(f"Failed to locate built-in sources: {e}")
            return 0

        count = 0
        crud_items: list[tuple[str, str, dict[str, Any]]] = []
        for resource in builtins_dir.iterdir():
            if not resource.name.endswith(".yaml"):
                continue

            source_name = resource.name.removesuffix(".yaml")
            if not overwrite and self.source_exists(source_name):
                logger.debug(f"Built-in source {source_name!r} already exists, skipping")
                continue

            try:
                content = resource.read_text()
                if self._crud is not None:
                    crud_items.append((_SOURCES_CLASS, source_name, yaml_load_string(content)))
                else:
                    dest = self._sources_directory / resource.name
                    dest.write_text(content)
                count += 1
                logger.info(f"Seeded built-in source: {source_name}")
            except Exception as e:
                logger.warning(f"Failed to seed source {source_name!r}: {e}")

        if crud_items:
            self._crud.put_many(
                crud_items,
                actor="engine",
                message="source: seed built-in sources",
            )
        if count > 0 and self._store is not None:
            self._store._refresh_all()

        return count

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def close(self) -> None:
        """Stop background refresh and cleanup."""
        if self._store is not None:
            self._store.stop()
