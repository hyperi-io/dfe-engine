"""Meta schema Registry - CRUD management for meta schema definitions.

The schemas tree the image ships sits under a DirectoryConfigStore. With gitops on,
the deploy repo's ``meta_schemas`` class is a layer over it, keyed by the same
paths: every API write lands there as one routed commit, and a read takes the
deploy repo's copy before the shipped one, so every engine replica serves one
answer. Without gitops, writes go to the directory itself.

Layout, under either root::

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

import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dulwich import porcelain as git
from dulwich.repo import InvalidUserIdentity, check_user_identity
from pydantic import ValidationError
from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.git_identity import commit_file
from dfe_engine.gitcrud.commit_policy import CommitPolicyError
from dfe_engine.schema.derived import DERIVED_PREFIX
from dfe_engine.schema.models import MetaSchema
from dfe_engine.schema.schema_loader import REGISTRIES_SUBDIR
from dfe_engine.yaml_utils import yaml_dump, yaml_load

if TYPE_CHECKING:
    from dfe_engine.gitcrud.routing import ReviewRouting
    from dfe_engine.hunts.deploy_repo import DeployRepoStore

META_CLASS = "meta_schemas"
"""The gitcrud resource class API-written meta schemas live in when gitops is on."""

_NOT_META = frozenset({REGISTRIES_SUBDIR, DERIVED_PREFIX})
"""Top-level directories of the schemas tree that hold no meta schema: allow-lists, derived."""


class SchemaError(Exception):
    """Base exception for schema registry errors."""


class SchemaNotFoundError(SchemaError):
    """Schema not found in the registry."""


class SchemaValidationError(SchemaError):
    """Schema definition failed validation."""


class SchemaShippedError(SchemaError):
    """The schema exists only in the image's tree, which no API write can remove."""


@dataclass(slots=True)
class SchemaEdit:
    """One schema file staged for a write, and the schema it saved as.

    Attributes:
        path: The YAML file to edit in place.
        saved: The schema as written, once the edit block has finished.
        document: The deploy-repo document as written, for a follow-on edit in the
            same request to start from where a review-routed write left main behind.
    """

    path: Path
    saved: MetaSchema | None = None
    document: dict[str, Any] | None = None


def _schema_location(schema_path: str) -> tuple[str, str]:
    """Split a registry key into parent path and schema name (YAML stem)."""
    parts = [p for p in schema_path.replace("\\", "/").split("/") if p]
    if not (parts):
        raise SchemaValidationError("Invalid empty schema path")
    if len(parts) == 1:
        return "", parts[0]
    return "/".join(parts[:-1]), parts[-1]


def canonical_schema_path(schema_path: str) -> str:
    """Normalize a registry key (forward slashes, no empty segments)."""
    parent, name = _schema_location(schema_path=schema_path)
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

    Provides CRUD operations, standard-scoped queries, and git-aware writes over
    the image's schemas tree, with the deploy repo layered over it when given.
    """

    _instance: SchemaRegistry | None = None

    def __init__(
        self,
        schemas_directory: str | Path,
        writable: bool | None = None,
        git_branch: str | None = None,
        git_push: bool = False,
        refresh_interval: int = 30,
        *,
        deploy_repo: DeployRepoStore | None = None,
    ) -> None:
        """Bind the registry to the schemas tree, and to the deploy repo when gitops is on.

        Args:
            schemas_directory: The schemas tree the image ships and the engine seeds.
            writable: Whether writes are allowed. None = auto-detect.
            git_branch: Git branch for writes. None = current branch.
            git_push: Auto-push after git commits.
            refresh_interval: Seconds between background cache refresh polls.
            deploy_repo: The deploy repo's ``meta_schemas`` class. When given, every
                write lands there and a read takes its copy first.
        """
        self._directory = Path(schemas_directory).expanduser().resolve(strict=False)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._deploy = deploy_repo

        self._store = DirectoryConfigStore(
            directory=self._directory,
            refresh_interval=refresh_interval,
            writable=writable,
            git_branch=git_branch,
            git_push=git_push,
        )
        self._store.start()

    # -----------------------------------------------------------------
    # Backend primitives
    # -----------------------------------------------------------------

    @staticmethod
    def _deploy_key(path: str) -> str:
        """The deploy-repo key for a schema path, refusing one the meta class cannot hold.

        Raises:
            SchemaValidationError: The path is empty, escapes the tree, or sits under
                ``registries/`` or ``derived/``.
        """
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            raise SchemaValidationError("Invalid empty schema path")
        for segment in parts:
            if segment in (".", "..") or "\x00" in segment:
                raise SchemaValidationError(f"Invalid schema path segment: {segment!r}")
        if parts[0] in _NOT_META:
            raise SchemaValidationError(f"Schema path must not be under {parts[0]}/")
        return "/".join(parts)

    def _deploy_doc(self, table: str) -> dict[str, Any] | None:
        """The deploy repo's copy of a schema, None off gitops or where it holds none."""
        if self._deploy is None:
            return None
        try:
            key = self._deploy_key(table)
        except SchemaValidationError:
            return None
        return self._deploy.get(key)

    def _raw(self, table: str) -> dict[str, Any] | None:
        """A schema's stored document: the deploy repo's copy, else the tree's."""
        held = self._deploy_doc(table)
        if held is not None:
            return held
        return self._store.get(table)

    def _tables(self) -> list[str]:
        """Every schema key, the deploy repo's first, each once."""
        tables: list[str] = []
        if self._deploy is not None:
            tables = [
                name for name in self._deploy.names() if name.split("/", 1)[0] not in _NOT_META
            ]
        held = set(tables)
        tables.extend(table for table in self._store.list_tables() if table not in held)
        return tables

    def _stored_file(self, table: str) -> Path:
        """The file a schema was read from, for its modified time."""
        if self._deploy_doc(table) is not None and self._deploy is not None:
            return self._deploy.directory / f"{table}.yaml"
        return self._yaml_path(table)

    def stored_config(self, path: str) -> dict[str, Any] | None:
        """A schema's document as stored, None when absent or the path is invalid.

        Args:
            path: The schema's registry path.
        """
        try:
            canonical = canonical_schema_path(path)
        except SchemaValidationError:
            return None
        return self._raw(self._table_name(canonical))

    @staticmethod
    def _draft_commit_message(
        table: str, action: str, description: str | None, created_by: str | None
    ) -> str:
        """Build the git commit subject for a schema change."""
        message = description or f"schema: {action} {table}"
        if created_by:
            message = f"{message} (by {created_by})"
        return message

    def _git_commit_and_push(
        self, yaml_path: Path, commit_msg: str, created_by: str | None = None
    ) -> None:
        """Commit a schema file change and push to the remote when configured."""
        commit_file(self._store, yaml_path, commit_msg, author=created_by)
        if self._store._git_push:
            self._store._git_push_remote()

    def _git_delete(self, yaml_path: Path, commit_msg: str, created_by: str | None = None) -> None:
        """Stage and commit a schema file deletion, pushing if configured."""
        repo = self._store._repo
        if repo is None:
            yaml_path.unlink(missing_ok=True)
            return
        try:
            repo_root = Path(repo.path).resolve(strict=False)
            rel_path = str(yaml_path.resolve(strict=False).relative_to(repo_root))
            yaml_path.unlink(missing_ok=True)
            git.rm(repo, paths=[rel_path])
            self._git_commit_and_push(
                commit_msg=commit_msg, created_by=created_by, yaml_path=yaml_path
            )
        except (git.Error, ValueError, OSError) as e:
            logger.error(f"Git delete failed: {e}")
            yaml_path.unlink(missing_ok=True)

    @staticmethod
    def _table_name(path: str) -> str:
        """Map path to a DirectoryConfigStore table name."""
        return f"{path}"

    def _validate_created_by(self, created_by: str | None) -> None:
        """Reject a git author string that is not 'username <email>'."""
        if not (self._store.is_git and created_by):
            return
        try:
            check_user_identity(created_by.encode())
        except InvalidUserIdentity as e:
            raise SchemaValidationError(
                f"Invalid 'created_by' identity (expected 'username <email>'): {created_by!r}"
            ) from e

    def _yaml_path(self, table: str) -> Path:
        """Filesystem path for a schema table key (e.g. ``aws/cloudtrail`` -> ``.../aws/cloudtrail.yaml``).

        Slashes (and backslashes, normalized to slashes) separate nested directories so keys like `acme/cloudtrail` and `contoso/cloudtrail` map to different files.
        """
        parts = [part for part in table.replace("\\", "/").split("/") if (part)]
        if not (parts):
            raise SchemaValidationError("Invalid empty schema path")
        for segment in parts:
            if segment in (".", ".."):
                raise SchemaValidationError(f"Invalid schema path segment: {segment!r}")
            if "\x00" in segment:
                raise SchemaValidationError("Invalid schema path: null byte in segment")
        # registries/ holds the engine's allow-lists, which a schema write must not replace.
        if parts[0] == REGISTRIES_SUBDIR:
            raise SchemaValidationError(f"Schema path must not be under {REGISTRIES_SUBDIR}/")
        if len(parts) == 1:
            candidate = self._directory / f"{parts[0]}.yaml"
        else:
            candidate = self._directory.joinpath(*parts[:-1]) / f"{parts[-1]}.yaml"

        base = self._directory.resolve(strict=False)
        resolved = candidate.resolve(strict=False)
        if not (resolved.is_relative_to(base)):
            raise SchemaValidationError("Schema path escapes schemas directory")
        return candidate

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
                raise SchemaError("'schemas_directory' is required on first call to get_instance()")
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

    def _parse_meta_schema(self, table: str, config_data: dict[str, Any]) -> MetaSchema:
        return MetaSchema.model_validate(coerce_common_header_legacy_versions(table, config_data))

    # -----------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------

    def get_schema_current_version(self, path: str) -> str:
        """Return ``current`` from the stored YAML without full MetaSchema validation."""
        table = self._table_name(path)
        config_data = self._raw(table)
        if config_data is None:
            raise SchemaNotFoundError(f"Schema not found: '{path}'")
        current = config_data.get("current")
        if not isinstance(current, str) or not current.strip():
            raise SchemaValidationError(f"Schema '{path}' has no valid current version pointer")
        return current

    def schema_version_exists(self, path: str, version: str) -> bool:
        """Whether ``version`` is already present in the stored YAML (raw view)."""
        table = self._table_name(path)
        config_data = self._raw(table)
        if not isinstance(config_data, dict):
            return False
        versions = config_data.get("versions")
        return isinstance(versions, dict) and version in versions

    def _publish(
        self,
        key: str,
        config_data: dict[str, Any],
        *,
        created_by: str | None,
        routing: ReviewRouting | None,
    ) -> None:
        """Commit one schema document to the deploy repo, routed by posture.

        Raises:
            SchemaValidationError: The path cannot name a deploy-repo document.
        """
        if self._deploy is None:
            raise SchemaError("no deploy repo backend")
        try:
            outcome = self._deploy.put(key, config_data, actor=created_by or "engine")
        except CommitPolicyError as exc:
            raise SchemaValidationError(f"Invalid schema path {key!r}: {exc}") from exc
        if routing is not None:
            routing.record(outcome)

    def save_schema(
        self,
        meta_schema: MetaSchema | dict[str, Any],
        created_by: str | None = None,
        description: str | None = None,
        *,
        routing: ReviewRouting | None = None,
    ) -> MetaSchema:
        """Save a meta schema definition.

        Args:
            meta_schema: The schema, its ``path`` naming where it is stored.
            created_by: The git author, ``username <email>`` for a directory in git.
            description: The commit subject for a directory in git.
            routing: Collects where a deploy-repo write landed.

        Returns:
            The schema as saved.

        Raises:
            SchemaValidationError: The schema or its path is invalid.
        """
        if isinstance(meta_schema, dict):
            table = str(meta_schema.get("path") or "")
            try:
                meta_schema = self._parse_meta_schema(table, meta_schema)
            except Exception as e:
                raise SchemaValidationError(f"Invalid schema definition: {e}") from e

        if not (meta_schema.path):
            raise SchemaValidationError("'MetaSchema.path' is required when saving")

        config_data = meta_schema.to_yaml_dict()
        if self._deploy is not None:
            key = self._deploy_key(canonical_schema_path(meta_schema.path))
            self._publish(key, config_data, created_by=created_by, routing=routing)
            logger.info(f"Saved schema {key!r} -> deploy repo")
            return meta_schema

        self._validate_created_by(created_by=created_by)

        table = self._table_name(path=meta_schema.path)

        yaml_path = self._yaml_path(table=meta_schema.path)
        yaml_path.parent.mkdir(parents=True, exist_ok=True)

        yaml_dump(data=config_data, dest=yaml_path)

        # Git commit if git-aware
        if self._store.is_git:
            commit_msg = self._draft_commit_message(
                action="update", created_by=created_by, description=description, table=table
            )
            self._git_commit_and_push(
                commit_msg=commit_msg, created_by=created_by, yaml_path=yaml_path
            )

        # Refresh cache
        self._store._refresh_all()

        logger.info(f"Saved schema {table!r} -> {yaml_path!r}")
        return meta_schema

    def get_schema(self, path: str) -> MetaSchema:
        """Get a meta schema, the deploy repo's copy before the tree's."""
        table = self._table_name(path)
        config_data = self._raw(table)
        if config_data is None:
            desc = f"{path}"
            raise SchemaNotFoundError(f"Schema not found: {desc!r}")

        schema = MetaSchema.model_validate(config_data)
        schema.path = path
        return schema

    def notify_schema_file_updated(
        self,
        path: str,
        *,
        description: str | None = None,
        created_by: str | None = None,
    ) -> MetaSchema:
        """Refresh cache (and git-commit) after a direct on-disk schema YAML write.

        Directory backend only: a deploy-repo registry takes its edits through
        :meth:`editing`.

        Raises:
            SchemaError: The deploy repo is the backend.
            SchemaNotFoundError: No file is stored at *path*.
        """
        if self._deploy is not None:
            raise SchemaError("a deploy-repo schema registry takes edits through editing()")
        table = self._table_name(path)
        yaml_path = self._yaml_path(path)
        if not yaml_path.exists():
            raise SchemaNotFoundError(f"Schema not found: {path!r}")

        self._validate_created_by(created_by=created_by)

        if self._store.is_git:
            commit_msg = self._draft_commit_message(table, "update", description, created_by)
            self._git_commit_and_push(yaml_path, commit_msg, created_by)

        self._store._refresh_all()
        logger.info(f"Schema file updated {table!r} -> {yaml_path}")
        return self.get_schema(path)

    @contextmanager
    def editing(
        self,
        path: str,
        *,
        description: str | None = None,
        created_by: str | None = None,
        routing: ReviewRouting | None = None,
        base: dict[str, Any] | None = None,
    ) -> Generator[SchemaEdit]:
        """Stage one schema file for an in-place edit, and store it when the block ends.

        A directory registry hands over the file itself and commits it afterwards,
        as :meth:`notify_schema_file_updated` does. A deploy-repo registry hands over
        a private copy of the stored document, the deploy repo's or else the tree's,
        and commits what the block leaves there as one routed write. A block that
        raises stores nothing.

        Args:
            path: The schema's registry path.
            description: The commit subject for a directory in git.
            created_by: The git author.
            routing: Collects where a deploy-repo write landed.
            base: The document a deploy-repo edit starts from in place of the stored
                one, such as an earlier edit's ``document`` that review left off main.

        Yields:
            The staged file, whose ``saved`` is set once the block finishes.

        Raises:
            SchemaNotFoundError: The block left no file to store.
            SchemaValidationError: The path is invalid, or what the block left is
                not a meta schema.
        """
        canonical = canonical_schema_path(path)
        if self._deploy is None:
            edit = SchemaEdit(path=self._yaml_path(canonical))
            yield edit
            edit.saved = self.notify_schema_file_updated(
                canonical, description=description, created_by=created_by
            )
            return

        key = self._deploy_key(canonical)
        with tempfile.TemporaryDirectory(prefix="dfe-schema-edit-") as staging:
            staged = Path(staging) / "schema.yaml"
            current = base if base is not None else self._raw(key)
            if current is not None:
                yaml_dump(current, staged)
            edit = SchemaEdit(path=staged)
            yield edit
            if not staged.is_file():
                raise SchemaNotFoundError(f"Schema not found: {canonical!r}")
            doc = yaml_load(staged)
            if not isinstance(doc, dict):
                raise SchemaValidationError(f"Schema {canonical!r} is not a mapping")
            try:
                saved = self._parse_meta_schema(key, doc)
            except ValidationError as exc:
                raise SchemaValidationError(f"Invalid schema definition: {exc}") from exc
            self._publish(key, doc, created_by=created_by, routing=routing)
        saved.path = canonical
        edit.saved = saved
        edit.document = doc

    @contextmanager
    def schema_file(self, path: str) -> Generator[Path]:
        """A readable YAML file holding a schema's stored document.

        The tree's own file for a directory registry, and a private copy of the
        deploy repo's or the tree's document otherwise.

        Raises:
            SchemaNotFoundError: Nothing is stored at *path*.
        """
        canonical = canonical_schema_path(path)
        if self._deploy is None:
            yield self._yaml_path(canonical)
            return
        current = self._raw(canonical)
        if current is None:
            raise SchemaNotFoundError(f"Schema not found: {canonical!r}")
        with tempfile.TemporaryDirectory(prefix="dfe-schema-read-") as staging:
            staged = Path(staging) / "schema.yaml"
            yaml_dump(current, staged)
            yield staged

    def delete_schema(
        self,
        path: str,
        *,
        description: str | None = None,
        created_by: str | None = None,
        routing: ReviewRouting | None = None,
    ) -> None:
        """Delete a meta schema.

        On the deploy repo this removes the deployment's copy, so a schema the image
        also ships reads as shipped again.

        Raises:
            SchemaShippedError: Only the image's tree holds the schema.
        """
        if self._deploy is not None:
            key = self._deploy_key(canonical_schema_path(path))
            outcome = self._deploy.delete(key, actor=created_by or "engine")
            if outcome is None:
                if self._store.get(self._table_name(path)) is not None:
                    raise SchemaShippedError(
                        f"Schema {path!r} ships with the image and is not stored in the "
                        "deploy repo, so there is nothing to delete"
                    )
                logger.warning(f"Schema does not exist: {path!r}")
                return
            if routing is not None:
                routing.record(outcome)
            logger.info(f"Deleted schema {key!r} from the deploy repo")
            return

        table = self._table_name(path)
        yaml_path = self._yaml_path(path)

        if not (yaml_path.exists()):
            logger.warning(f"Schema file does not exist: {yaml_path!r}")
            return

        self._validate_created_by(created_by=created_by)

        if self._store.is_git:
            commit_msg = self._draft_commit_message(table, "delete", description, created_by)
            self._git_delete(yaml_path=yaml_path, commit_msg=commit_msg, created_by=created_by)
        else:
            yaml_path.unlink(missing_ok=True)

        with self._store._lock:
            self._store._cache.pop(table, None)

        logger.info(f"Deleted schema {table!r}")

    def find_schema_at_location(self, schema_path: str) -> str | None:
        """Return an existing table key that occupies the same path location, if any.

        Uses DirectoryConfigStore table keys only (no YAML parsing). Registry keys
        match on-disk layout; ``MetaSchema.path`` is not persisted in YAML files.
        """
        parent, name = _schema_location(schema_path)
        for table in self._tables():
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
        for table in self._tables():
            if path and table != path:
                continue

            config_data = self._raw(table)
            if config_data is None:
                continue

            try:
                schema = self._parse_meta_schema(table, config_data)
            except ValidationError as e:
                # The schemas root holds non-schema YAML too (a schemas repo ships
                # .github/, argocd/, docs/), so a non-MetaSchema file is an expected
                # exclusion on this read path, not a per-call error.
                logger.debug(f"Non-schema file {table!r} excluded from list: {e}")
                continue
            except Exception as e:
                logger.warning(f"Failed to load schema {table!r} (excluded from list): {e}")
                continue

            # Resolve YAML path for modified-time (use store table key, not list_schemas filter)
            schema_rel = getattr(schema, "path", None) or table
            yaml_path = self._stored_file(schema_rel)
            try:
                stat = yaml_path.stat()
                updated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            except OSError:
                updated_at = ""

            # 'current' always names a real version; fall back to the first if it ever doesn't.
            current_version = schema.versions.get(schema.current)
            version = current_version or next(iter(schema.versions.values()))

            results.append(
                {
                    "path": schema_rel,
                    "resource_type": schema.resource_type,
                    "current": schema.current,
                    "versions": list(schema.versions.keys()),
                    "column_count": len(version.columns),
                    "updated_at": updated_at,
                }
            )

        return results

    # -----------------------------------------------------------------
    # Change Callbacks & Git (passthrough)
    # -----------------------------------------------------------------

    def on_change(self, path: str, callback: Any) -> None:
        """Register a callback for when a schema in the image's tree changes."""
        table = self._table_name(path)
        self._store.on_change(table, callback)

    @property
    def is_git(self) -> bool:
        """Whether writes land as git commits.

        The deploy repo always commits. A directory answers True when it sits
        INSIDE a git repository: scalo walks up, so a directory under a gitops
        checkout answers True without being a repository root itself.
        """
        if self._deploy is not None:
            return True
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
