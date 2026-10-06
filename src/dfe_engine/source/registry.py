"""Source Registry -- CRUD management for Source definitions.

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

from pydantic import ValidationError
from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.core_resources.yaml_resource_type import (
    CORE_RESOURCE_MUTATION_MESSAGE,
    config_is_core,
)
from dfe_engine.git_identity import COMMITTER_IDENTITY, commit_file
from dfe_engine.source.alignment import MixedConnectorTypesError, require_one_type
from dfe_engine.source.engine_registry import InvalidEngineError
from dfe_engine.source.models import (
    DEFAULT_LANDING_LABEL,
    RULELESS_OPERATORS,
    Source,
    SourceMatch,
    SourceVersion,
    SourceWriteRequest,
    apply_source_write_update,
    draft_build_version_to_invalidate,
    engine_registry,
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


def _first_error_message(exc: ValidationError) -> str:
    """The readable half of a pydantic error, so the API can hand it to a caller.

    Keeps the explanation a field validator wrote and drops the report scaffolding
    around it; a caller told only "1 validation error for Source" learns nothing.
    """
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "")).removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


class SourceCoreResourceError(SourceValidationError):
    """A write targets a stored source the engine owns."""

    def __init__(self, *, action: str, source: str) -> None:
        self.source = source
        self.action = action
        super().__init__(
            f"{CORE_RESOURCE_MUTATION_MESSAGE}: cannot {action} source {source!r}, which the "
            "engine owns and reconciles from the deployment's own settings."
        )


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
    #
    # ALL backend branching lives here: the CRUD methods above/below call
    # these and never test self._crud themselves.
    # -----------------------------------------------------------------

    def _require_store(self) -> DirectoryConfigStore:
        """Narrow the Optional store once: the directory backend always builds one."""
        if self._store is None:
            raise SourceRegistryError(
                "no DirectoryConfigStore backend (the gitcrud backend is active)"
            )
        return self._store

    def _names(self) -> list[str]:
        """All stored source names."""
        if self._crud is not None:
            return self._crud.list(_SOURCES_CLASS)
        return list(self._require_store().list_tables())

    def _get_raw(self, source_name: str) -> dict[str, Any] | None:
        """Raw stored doc for a source, or None when absent."""
        if self._crud is not None:
            from dfe_engine.gitcrud.engine import ResourceNotFoundError

            try:
                return self._crud.get(_SOURCES_CLASS, source_name)
            except ResourceNotFoundError:
                return None
        return self._require_store().get(source_name)

    def _put_raw(
        self,
        name: str,
        doc: dict[str, Any],
        *,
        created_by: str | None,
        message: str,
    ) -> str:
        """Write one stored source doc; returns the destination label for the save log.

        gitcrud: the doc gains the universal gitcrud ``metadata`` block (derived
        from the doc's own description/display_name) and the write is ONE commit
        in the deploy repo. Directory backend: plain YAML write, git commit when
        the directory is a repo, then a cache refresh.
        """
        if self._crud is not None:
            from dfe_engine.gitcrud.metadata import ResourceMetadata, with_metadata

            stored = with_metadata(
                doc,
                ResourceMetadata(
                    description=doc.get("description") or "",
                    display_name=doc.get("display_name"),
                ),
            )
            self._crud.put(
                _SOURCES_CLASS,
                name,
                stored,
                actor=created_by or "engine",
                message=message,
            )
            return "gitcrud config/sources"

        store = self._require_store()
        yaml_path = self._sources_directory / f"{name}.yaml"
        yaml_dump(doc, yaml_path)
        if store.is_git:
            commit_file(store, yaml_path, message, author=created_by)
            if store._git_push:
                store._git_push_remote()
        store._refresh_all()
        return str(yaml_path)

    def _delete_raw(self, name: str, *, created_by: str | None, message: str) -> bool:
        """Remove one stored source doc; False when it did not exist.

        gitcrud: one attributed commit ("(by <created_by>)" suffix, like save).
        Directory backend: unlink + engine-identity commit when git - it never
        carried delete attribution, so ``created_by`` is deliberately unused there.
        """
        if self._crud is not None:
            from dfe_engine.gitcrud.engine import ResourceNotFoundError

            commit_msg = f"{message} (by {created_by})" if created_by else message
            try:
                self._crud.delete(
                    _SOURCES_CLASS,
                    name,
                    actor=created_by or "engine",
                    message=commit_msg,
                )
            except ResourceNotFoundError:
                return False
            return True

        store = self._require_store()
        yaml_path = self._sources_directory / f"{name}.yaml"
        if not yaml_path.exists():
            return False

        # Git rm + commit if git-aware
        if store.is_git and store._repo is not None:
            try:
                from dulwich import porcelain as git

                repo_root = Path(store._repo.path)
                rel_path = str(yaml_path.relative_to(repo_root))

                yaml_path.unlink()
                git.rm(store._repo, paths=[rel_path])
                git.commit(
                    store._repo,
                    author=COMMITTER_IDENTITY.encode("utf-8"),
                    committer=COMMITTER_IDENTITY.encode("utf-8"),
                    message=message.encode(),
                )
                if store._git_push:
                    store._git_push_remote()
            except Exception as e:
                logger.error(f"Git delete failed: {e}")
        else:
            yaml_path.unlink()

        # Remove from cache
        with store._lock:
            store._cache.pop(name, None)
        return True

    def _put_many_raw(
        self,
        items: list[tuple[str, str]],
        *,
        created_by: str,
        message: str,
    ) -> None:
        """Bulk-write raw YAML texts (name, text): ONE commit on gitcrud (put_many).

        The directory backend copies the files verbatim (seeding is bootstrap,
        not an operator edit - no per-file commit) and refreshes the cache.
        """
        if not items:
            return
        if self._crud is not None:
            from dfe_engine.yaml_utils import yaml_load_string

            self._crud.put_many(
                [(_SOURCES_CLASS, name, yaml_load_string(text)) for name, text in items],
                actor=created_by,
                message=message,
            )
            return
        store = self._require_store()
        for name, text in items:
            (self._sources_directory / f"{name}.yaml").write_text(text)
        store._refresh_all()

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

    def core_source_names(self) -> list[str]:
        """Every stored engine-owned source, by name.

        Reads raw docs rather than going through ``list_sources``, which drops any
        source the model cannot parse -- a core one dropped there would be invisible
        to the seeder and still refused by the gates, so unreachable from both sides.
        """
        return sorted(name for name in self._names() if self.is_core(name))

    def is_core(self, source_name: str) -> bool:
        """Whether the STORED source is engine-owned, whatever an incoming body claims.

        Reads the raw doc, so it answers for a stored source the model can no longer
        parse -- which is the one the gates most need to refuse.
        """
        return config_is_core(self._get_raw(source_name))

    def validate_source(self, source: Source) -> None:
        """Run every save-time check without writing, for a caller that writes more than one thing.

        Raises:
            SourceValidationError: The source would be refused by ``save_source``.
        """
        self._validate_save(source)

    def save_source(
        self,
        source: Source | dict[str, Any],
        created_by: str | None = None,
        description: str | None = None,
        *,
        core_reconcile: bool = False,
    ) -> Source:
        """Save a source definition to the YAML directory.

        Validates the source (Pydantic + uniqueness + match conflicts)
        before writing. If the directory is a git repo, changes are
        auto-committed.

        Args:
            source: Source model or dict.
            created_by: Username/identity of who made the change.
            description: Description of the change.
            core_reconcile: Engine-only. Permits the write that keeps a core source
                in step with the deployment's settings. No request path sets it.

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
        self._validate_save(source, core_reconcile=core_reconcile)

        # Serialize and write
        config_data = source.to_yaml_dict()

        commit_msg = description or f"source: update {source.source}"
        if created_by:
            commit_msg = f"{commit_msg} (by {created_by})"

        dest = self._put_raw(source.source, config_data, created_by=created_by, message=commit_msg)
        logger.info(f"Saved source {source.source!r} -> {dest}")
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

        # Building the Source is where the name rules are enforced, so a bad name
        # has to surface as a validation error the API can map to a 422 rather
        # than as a raw pydantic error nothing catches.
        try:
            source = source_from_write(write, source_name=write.source)
        except ValidationError as e:
            raise SourceValidationError(_first_error_message(e)) from e
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

    def delete_source(
        self,
        source_name: str,
        created_by: str | None = None,
        *,
        core_reconcile: bool = False,
    ) -> None:
        """Delete a source definition.

        Removes the YAML file and commits the deletion if git-aware.

        Args:
            source_name: The _source label.
            created_by: Username/identity of who deleted it (gitcrud commit
                attribution, same as save).
            core_reconcile: Engine-only. Permits removing a core source the
                settings no longer name. No request path sets it.

        Raises:
            SourceCoreResourceError: The stored source is engine-owned.
        """
        # Its own gate: delete_source does not pass through _validate_save.
        if not (core_reconcile) and self.is_core(source_name):
            raise SourceCoreResourceError(action="delete", source=source_name)

        deleted = self._delete_raw(
            source_name,
            created_by=created_by,
            message=f"source: delete {source_name}",
        )
        if not deleted:
            logger.warning(f"Source does not exist: {source_name!r}")
            return
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
                    "resource_type": source.resource_type,
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
                    "origin": source.origin,
                    "current_table_topic_type": source.table_topic_type,
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

    def _validate_save(self, source: Source, *, core_reconcile: bool = False) -> None:
        """Validate before saving: not core, unique source, runnable flow, no match conflicts."""
        # Read the STORED doc, not the incoming one: an operator body claiming resource_type is rejected at the model, and a body that omits it must not be able to demote a core source by overwriting it.
        if not (core_reconcile) and self.is_core(source.source):
            raise SourceCoreResourceError(action="modify", source=source.source)

        candidate_version = source.versions.get(source.current)
        candidate_match = candidate_version.match if candidate_version else None

        # A DISABLED source skips every gate below - disabling is exactly how an
        # operator retires a stored source the current rules would reject;
        # active/dormant still reject (a dormant source may activate later).
        if source.state != "disabled":
            self._validate_match_operator(source, candidate_match)
            self._validate_fetcher_routes(source, candidate_version)
            self._validate_fetcher_types(source, candidate_version)
            self._validate_engine_arguments(source, candidate_version)
            self._validate_flow(source)
            self._validate_instance_room(source)

        for table in self._names():
            if table == source.source:
                continue  # Same source (update)

            config_data = self._get_raw(table)
            if config_data is None:
                continue

            try:
                existing = Source.model_validate(config_data)
            except Exception:
                logger.warning(f"Failed to parse source {table!r}, skipping its conflict check")
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

    @staticmethod
    def _validate_match_operator(source: Source, match: SourceMatch | None) -> None:
        """A receiver-routed source's operator must be one the receiver can act on.

        ``always`` is the default flow's rule and compiles to the receiver's
        ``default_source`` rather than to a rule, so it is legal only for the
        reserved source that flow belongs to. Everything else must map onto a
        hot-path router mode; the operators that do not are a documented
        receiver gap.
        """
        if match is None:
            return
        # Lazy import: source_routing imports this module (cycle).
        from dfe_engine.services.source_routing import (
            UnsupportedMatchOperatorError,
            operator_mode,
        )

        if match.operator in RULELESS_OPERATORS:
            if source.source != DEFAULT_LANDING_LABEL:
                raise SourceValidationError(
                    f"source {source.source!r}: match operator {match.operator!r} matches "
                    f"every record, so it is reserved for the {DEFAULT_LANDING_LABEL!r} "
                    "source that defines the main flow"
                )
            return
        if operator_mode(match.operator) is None:
            raise SourceValidationError(
                str(UnsupportedMatchOperatorError(source.source, match.operator))
            )

    @staticmethod
    def _validate_fetcher_routes(source: Source, version: SourceVersion | None) -> None:
        """A fetcher route sends records ELSEWHERE, so it may not name its own source.

        That the named source exists is checked when the fetcher instance is
        compiled: a route may legitimately be written before its target is.
        """
        fetcher = version.fetcher if version else None
        for route in fetcher.routes if fetcher else ():
            if route.source == source.source:
                raise SourceValidationError(
                    f"source {source.source!r}: a fetcher route names its own source; "
                    "records with no route already land there"
                )

    @staticmethod
    def _validate_fetcher_types(source: Source, version: SourceVersion | None) -> None:
        """A source is one connector type, however many connections it polls.

        One schema per source is what keeps its table one shape, so the write is
        refused here; dfe-fetcher takes any mix and is never asked to.
        """
        from dfe_engine.settings import get_settings

        fetcher = version.fetcher if version else None
        if fetcher is None:
            return
        try:
            require_one_type(source.source, {fetcher.source_type: fetcher.config}, get_settings())
        except MixedConnectorTypesError as exc:
            raise SourceValidationError(str(exc)) from exc

    @staticmethod
    def _validate_engine_arguments(source: Source, version: SourceVersion | None) -> None:
        """A set engine must follow its variant's argument rule; a blank one follows the default."""
        engine = version.effective_schema().engine if version else ""
        if not (engine):
            return
        try:
            engine_registry().validate_arguments(engine)
        except InvalidEngineError as exc:
            raise SourceValidationError(f"source {source.source!r}: {exc}") from exc

    def _validate_flow(self, source: Source) -> None:
        """The source's stages must be runnable on this deployment.

        The resolver holds every transport rule (what the deployment offers, what
        each app carries, what archiving needs), so the save path asks it rather
        than restating any of them. An archive nothing would copy, the source's own
        or a route target's, is refused here and not in the resolver, which also
        compiles the sources already stored.
        """
        from dfe_engine.settings import get_settings
        from dfe_engine.source.flow import FlowError, archive_gap, resolve_flow, route_archive_gap

        settings = get_settings()
        try:
            flow = resolve_flow(source, settings)
        except FlowError as exc:
            raise SourceValidationError(str(exc)) from exc
        gap = archive_gap(flow)
        if gap is not None:
            raise SourceValidationError(gap)

        fetcher = source.fetcher
        for route in fetcher.routes if fetcher else ():
            # A target not yet written, or one that cannot run, is the compile's to report.
            try:
                target = resolve_flow(self.get_source(route.source), settings)
            except SourceNotFoundError, ValidationError, FlowError:
                continue
            gap = route_archive_gap(flow, target)
            if gap is not None:
                raise SourceValidationError(gap)

    def _validate_instance_room(self, source: Source) -> None:
        """A stage needing its OWN deployment must have one free here.

        Kubernetes renders an Argo Application per overlay, so there is always
        room. Compose declares its services in a committed file and creates none
        at run time, so the first source to need a one-per-source app takes the
        resident container and the next is refused rather than stored and never
        run.
        """
        if self._crud is None:
            return
        from dfe_engine.appmgmt import instances
        from dfe_engine.settings import get_settings
        from dfe_engine.source.flow import (
            FETCHER_SERVICE,
            FlowError,
            resolve_flow,
            stage_instance_ceiling,
        )

        settings = get_settings()
        try:
            flow = resolve_flow(source, settings)
        except FlowError:  # _validate_flow already reported it
            return

        needed = {flow.transform.app} if flow.transform is not None else set()
        if source.fetcher is not None:
            needed.add(FETCHER_SERVICE)
        for service in sorted(needed):
            app = instances.instance_of(service, source.source)
            ceiling = stage_instance_ceiling(app.descriptor, settings)
            allowed, reason = instances.additional_instance_allowed(self._crud, app, ceiling)
            if not allowed:
                raise SourceValidationError(f"source {source.source!r}: {reason}")

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
        self._require_store().on_change(source_name, callback)

    # -----------------------------------------------------------------
    # Git Operations (passthrough)
    # -----------------------------------------------------------------

    @property
    def is_git(self) -> bool:
        """Whether the sources backend is git-native."""
        if self._crud is not None:
            return True
        return self._require_store().is_git

    @property
    def current_branch(self) -> str | None:
        """Current git branch name."""
        if self._crud is not None:
            return self._crud.repo.branch
        return self._require_store().current_branch

    def list_branches(self) -> list[str]:
        """List all git branches."""
        if self._crud is not None:
            raise SourceRegistryError(
                "branch operations are owned by the gitcrud routing layer on this backend"
            )
        return self._require_store().list_branches()

    def switch_branch(self, branch: str, create: bool = False) -> None:
        """Switch to a git branch. Refreshes cache after switch."""
        if self._crud is not None:
            raise SourceRegistryError(
                "branch operations are owned by the gitcrud routing layer on this backend"
            )
        self._require_store().switch_branch(branch, create=create)

    # -----------------------------------------------------------------
    # Built-in Sources
    # -----------------------------------------------------------------

    def seed_builtin_sources(self, overwrite: bool = False) -> int:
        """Seed the sources directory with the built-in source definitions.

        Copy-on-adopt: the definitions are data in dfe-schemas' ``sources/`` and
        are copied into the deployment's sources store. Non-destructive by
        default -- skips sources that already exist. On the gitcrud backend all
        seeds land in ONE commit.

        Args:
            overwrite: If True, overwrite existing source definitions.

        Returns:
            Number of sources seeded.
        """
        from dfe_engine.schema.plan import core_schemas_root
        from dfe_engine.source.core_sources import LANDING_SOURCE_FILE

        builtins_dir = core_schemas_root() / "sources"
        if not builtins_dir.is_dir():
            logger.warning(f"dfe-schemas ships no sources directory at {builtins_dir}")
            return 0

        items: list[tuple[str, str]] = []
        for resource in sorted(builtins_dir.iterdir()):
            # The landing definition is the engine's own: seed_core_sources fills
            # its name from clickhouse.landing_table, which this path cannot.
            if not resource.name.endswith(".yaml") or resource.name == LANDING_SOURCE_FILE:
                continue

            source_name = resource.name.removesuffix(".yaml")
            if not overwrite and self.source_exists(source_name):
                logger.debug(f"Built-in source {source_name!r} already exists, skipping")
                continue

            try:
                items.append((source_name, self._named(resource, source_name)))
                logger.info(f"Seeded built-in source: {source_name}")
            except Exception as e:
                logger.warning(f"Failed to seed source {source_name!r}: {e}")

        self._put_many_raw(
            items,
            created_by="engine",
            message="source: seed built-in sources",
        )
        return len(items)

    @staticmethod
    def _named(resource: Path, source_name: str) -> str:
        """The shipped definition with its ``source`` name filled from the filename.

        dfe-schemas names a source by its file so one definition can be adopted
        under whatever name a deployment gives it; the stored copy carries the
        name, because every read path validates a Source model.
        """
        from dfe_engine.yaml_utils import yaml_dump_string, yaml_load_string

        doc = yaml_load_string(resource.read_text(encoding="utf-8")) or {}
        doc.setdefault("source", source_name)
        return yaml_dump_string(doc)

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def close(self) -> None:
        """Stop background refresh and cleanup."""
        if self._store is not None:
            self._store.stop()
