"""Meta schema Registry - CRUD management for meta schema definitions.

Two stores behind one surface:

- **gitcrud** (gitops on): the schemas an operator writes live in the deploy repo's
  ``config/schemas/definitions/`` (ResourceClass ``meta_schemas``), so each write is
  one git commit and a production+team write goes to a review branch. Reads see the
  deploy repo first, then the schemas tree.
- **directory** (standalone): DirectoryConfigStore over the schemas tree, which is
  both the read and the write location.

Directory layout of the schemas tree::

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

import copy
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.gitcrud.routing import GovernedWrites, ReviewRouting, WritePosture
from dfe_engine.names import InvalidNameError
from dfe_engine.schema.models import MetaSchema
from dfe_engine.schema.schema_loader import REGISTRIES_SUBDIR, resolve_schema_yaml_path
from dfe_engine.yaml_utils import yaml_dump, yaml_load

if TYPE_CHECKING:
    from dfe_engine.gitcrud.engine import GitCrud

META_CLASS = "meta_schemas"


class SchemaError(Exception):
    """Base exception for schema registry errors."""


def _class_directory() -> str:
    """The deploy-repo directory the ``meta_schemas`` class declares."""
    from dfe_engine.gitcrud.registry import default_registry

    return default_registry().get(META_CLASS).directory


def meta_deploy_root(deploy_schemas_root: Path | None, schemas_root: Path | None) -> Path | None:
    """Where a deployment's own meta-schema documents sit, or None when they sit in the schemas tree.

    ``deploy_schemas_root`` is the deploy repo's ``config/schemas`` (what
    ``derived_reference_root`` returns with gitops on). It equals the schemas tree
    when gitops is off, which leaves no second root.
    """
    if deploy_schemas_root is None:
        return None
    if schemas_root is not None and deploy_schemas_root.resolve() == schemas_root.resolve():
        return None
    return deploy_schemas_root / Path(_class_directory()).name


def resolve_meta_reference(
    reference: str, schemas_root: Path, deploy_root: Path | None = None
) -> Path:
    """The file a meta-schema reference names: the deploy repo's copy first, then the schemas tree.

    The same precedence ``SchemaRegistry`` reads in, so a source never pins a
    schema the registry reports missing. A miss returns the schemas tree's candidate.
    """
    if deploy_root is not None:
        candidate = resolve_schema_yaml_path(deploy_root, reference)
        if candidate.is_file():
            return candidate
    return resolve_schema_yaml_path(schemas_root, reference)


def _git_would_record(path: Path) -> bool:
    """Whether *path* sits in a git checkout that does not ignore it.

    An ignored scratch directory (``tmp/``) is not the worktree. A path with no
    checkout above it is not one either. Anything else would be a file git can
    add on the current branch.
    """
    for parent in (path, *path.parents):
        if not (parent / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(parent), "check-ignore", "-q", "--", str(path)],
            capture_output=True,
            check=False,
        )
        return result.returncode != 0
    return False


class SchemaNotFoundError(SchemaError):
    """Schema not found in the registry."""


class SchemaValidationError(SchemaError):
    """Schema definition failed validation."""


def _schema_location(schema_path: str) -> tuple[str, str]:
    """Split a registry key into parent path and schema name (YAML stem)."""
    parts = [p for p in schema_path.replace("\\", "/").split("/") if p]
    if not (parts):
        raise SchemaValidationError("Invalid empty schema path")
    for segment in parts:
        if segment in (".", ".."):
            raise SchemaValidationError(f"Invalid schema path segment: {segment!r}")
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

    Backed by DirectoryConfigStore (YAML directory as SSoT).
    Provides CRUD operations and schema queries by path.
    """

    _instance: SchemaRegistry | None = None

    def __init__(
        self,
        schemas_directory: str | Path,
        writable: bool | None = None,
        refresh_interval: int = 30,
        fixtures_directory: str | Path | None = None,
        *,
        crud: GitCrud | None = None,
        posture: WritePosture | None = None,
    ) -> None:
        """Bind the registry to the schemas tree and, with gitops on, the deploy repo.

        Args:
            schemas_directory: The schemas tree. Read always, written only without ``crud``.
            writable: Passed to the directory store.
            refresh_interval: Seconds between directory store refreshes.
            fixtures_directory: Where e2e schema fixtures sit, outside the tree.
            crud: Governed Ops engine over the deploy repo. Writes then land in
                ``config/schemas/definitions/``, one git commit each.
            posture: Where a gitcrud write lands. A production+team write goes to a
                review branch. None reads the posture from the loaded settings, with no forge.
        """
        self._crud = crud
        self._posture = posture
        self._deploy_directory = (
            crud.repo_path / crud.resource_class(META_CLASS).directory if crud is not None else None
        )
        self._directory = Path(schemas_directory).expanduser().resolve(strict=False)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._fixtures = (
            Path(fixtures_directory).expanduser().resolve(strict=False)
            if fixtures_directory
            else None
        )
        if self._fixtures is not None and self._fixtures_overlap_tree():
            raise SchemaError("schema fixtures must sit outside the schema tree")

        self._store = DirectoryConfigStore(
            directory=self._directory,
            refresh_interval=refresh_interval,
            writable=writable,
        )
        self._store.start()

    @staticmethod
    def _table_name(path: str) -> str:
        """Map path to a DirectoryConfigStore table name."""
        return f"{path}"

    @property
    def fixtures_directory(self) -> Path | None:
        """Directory e2e schema fixtures write to, outside the schema tree."""
        return self._fixtures

    def _yaml_path(self, table: str) -> Path:
        """Filesystem path for a schema table key (e.g. ``aws/cloudtrail`` -> ``.../aws/cloudtrail.yaml``).

        Slashes (and backslashes, normalized to slashes) separate nested directories so keys like `acme/cloudtrail` and `contoso/cloudtrail` map to different files.
        """
        return self._yaml_under(self._directory, table)

    def _fixture_yaml(self, table: str) -> Path | None:
        """Path of one fixture schema, or None when no fixtures directory is configured."""
        if self._fixtures is None:
            return None
        return self._yaml_under(self._fixtures, table)

    def _yaml_under(self, root: Path, table: str) -> Path:
        """Map a schema path onto ``root`` and refuse a path that escapes it."""
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
            candidate = root / f"{parts[0]}.yaml"
        else:
            candidate = root.joinpath(*parts[:-1]) / f"{parts[-1]}.yaml"

        base = root.resolve(strict=False)
        resolved = candidate.resolve(strict=False)
        if not (resolved.is_relative_to(base)):
            raise SchemaValidationError("Schema path escapes schemas directory")
        return candidate

    def _assert_fixtures_isolated(self) -> None:
        """Refuse a fixture write that would land in the schema tree or a git worktree.

        The schema directory is the environment's copy of the packaged schemas.
        A fixture directory inside it, or inside a checkout git would record,
        dirties that tree. An ignored scratch directory (the e2e workspace) is
        the only place a fixture write is allowed.
        """
        fixtures = self._fixtures
        if fixtures is None:
            raise SchemaError(
                "schema fixtures directory is not configured; refusing to write into the schema tree"
            )
        if self._fixtures_overlap_tree():
            raise SchemaError("schema fixtures must sit outside the schema tree")
        if _git_would_record(fixtures.resolve(strict=False)):
            raise SchemaError("schema fixtures must not be written into a git worktree")

    def _fixtures_overlap_tree(self) -> bool:
        """Whether the fixtures directory is the schema tree, inside it, or holds it."""
        if self._fixtures is None:
            return False
        snapshot = self._directory.resolve(strict=False)
        resolved = self._fixtures.resolve(strict=False)
        return resolved.is_relative_to(snapshot) or snapshot.is_relative_to(resolved)

    def _fixture_tables(self) -> list[str]:
        """Schema paths stored in the fixtures directory."""
        root = self._fixtures
        if root is None or not root.is_dir():
            return []
        tables: list[str] = []
        for path in sorted(root.rglob("*.yaml")):
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            tables.append(relative.with_suffix("").as_posix())
        return tables

    def _fixture_data(self, table: str) -> dict[str, Any] | None:
        """Parsed fixture YAML for *table*, or None when that fixture is absent."""
        yaml_path = self._fixture_yaml(table)
        if yaml_path is None or not yaml_path.is_file():
            return None
        data = yaml_load(yaml_path)
        return data if isinstance(data, dict) else None

    @staticmethod
    def _readable_key(path: str) -> str | None:
        """The deploy-repo name for a registry path, or None when the path is unusable."""
        try:
            return canonical_schema_path(path)
        except SchemaValidationError:
            return None

    def _deploy_doc(self, path: str) -> dict[str, Any] | None:
        """The document the deploy repo holds at *path*, or None when it holds none."""
        crud = self._crud
        key = self._readable_key(path)
        if crud is None or key is None:
            return None
        from dfe_engine.gitcrud.engine import ResourceNotFoundError

        try:
            doc = crud.get(META_CLASS, key)
        except ResourceNotFoundError, InvalidNameError:
            return None
        return doc if isinstance(doc, dict) else None

    def _deploy_yaml(self, path: str) -> Path | None:
        """The deploy-repo file for *path*, or None where gitops is off or the path is unusable."""
        key = self._readable_key(path)
        if key is None or self._deploy_directory is None:
            return None
        return self._deploy_directory / f"{key}.yaml"

    def _stored(self, table: str) -> dict[str, Any] | None:
        """The document at *table* from the deploy repo, else the tree. Fixtures are not consulted."""
        doc = self._deploy_doc(table)
        if doc is not None:
            return doc
        data = self._store.get(table)
        return data if isinstance(data, dict) else None

    def _config(self, table: str) -> dict[str, Any] | None:
        """Schema document from the deploy repo or tree, falling back to a fixture of the same path."""
        data = self._stored(table)
        if data is not None:
            return data
        return self._fixture_data(table)

    def _known_tables(self) -> list[str]:
        """Tree tables, then deploy-repo and fixture paths the tree does not already have."""
        tables = list(self._store.list_tables())
        have = set(tables)
        deployed = self._crud.list(META_CLASS) if self._crud is not None else []
        for table in (*deployed, *self._fixture_tables()):
            if table not in have:
                tables.append(table)
                have.add(table)
        return tables

    def delete_fixture(self, path: str) -> bool:
        """Remove one fixture file and nothing in the schema tree.

        Returns whether a file was removed. Never commits.
        """
        yaml_path = self._fixture_yaml(path)
        if yaml_path is None or not yaml_path.is_file():
            return False
        yaml_path.unlink()
        return True

    @classmethod
    def get_instance(
        cls,
        schemas_directory: str | Path | None = None,
        writable: bool | None = None,
        refresh_interval: int = 30,
    ) -> SchemaRegistry:
        """Get singleton registry instance."""
        if cls._instance is None:
            if schemas_directory is None:
                raise SchemaError("'schemas_directory' is required on first call to get_instance()")
            cls._instance = SchemaRegistry(
                schemas_directory=schemas_directory,
                writable=writable,
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
        """Return ``current`` from cached YAML without full MetaSchema validation."""
        table = self._table_name(path)
        config_data = self._stored(table)
        if config_data is None:
            raise SchemaNotFoundError(f"Schema not found: '{path}'")
        current = config_data.get("current")
        if not isinstance(current, str) or not current.strip():
            raise SchemaValidationError(f"Schema '{path}' has no valid current version pointer")
        return current

    def schema_version_exists(self, path: str, version: str) -> bool:
        """Whether ``version`` is already present in the stored YAML (raw store view)."""
        table = self._table_name(path)
        config_data = self._stored(table)
        if config_data is None:
            return False
        versions = config_data.get("versions")
        return isinstance(versions, dict) and version in versions

    def _governed_write(
        self,
        path: str,
        write: Callable[[GitCrud, str, str, str, str], Any],
        *,
        created_by: str | None,
        message: str,
        routing: ReviewRouting | None,
    ) -> None:
        """Run a gitcrud write where the posture puts it, folding a review branch into *routing*.

        ``write`` receives the crud, the class key, the actor, the commit message and the branch.
        """
        crud = self._crud
        if crud is None:
            raise SchemaError("no gitcrud backend to write through")
        self._yaml_path(path)
        key = canonical_schema_path(path)
        actor = created_by or "engine"
        commit_message = f"{message} (by {created_by})" if created_by else message
        if self._posture is None:
            self._posture = WritePosture.from_loaded_settings()
        writes = GovernedWrites(crud, posture=self._posture)
        writes(
            META_CLASS,
            key,
            actor,
            commit_message,
            lambda branch: write(crud, key, actor, commit_message, branch),
        )
        if routing is not None:
            routing.fold(writes.routing)

    def _governed_put(
        self,
        path: str,
        doc: dict[str, Any],
        *,
        created_by: str | None,
        message: str,
        routing: ReviewRouting | None,
    ) -> None:
        """Write one deploy-repo document."""
        self._governed_write(
            path,
            lambda crud, key, actor, msg, branch: crud.put(
                META_CLASS, key, doc, actor=actor, message=msg, branch=branch
            ),
            created_by=created_by,
            message=message,
            routing=routing,
        )

    def save_schema(
        self,
        meta_schema: MetaSchema | dict[str, Any],
        *,
        created_by: str | None = None,
        description: str | None = None,
        routing: ReviewRouting | None = None,
    ) -> MetaSchema:
        """Save a meta schema definition.

        ``routing`` records the write when it went to a review branch instead of main.
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
        table = self._table_name(path=meta_schema.path)

        if self._crud is not None:
            self._governed_put(
                meta_schema.path,
                config_data,
                created_by=created_by,
                message=description or f"schema: save {canonical_schema_path(meta_schema.path)}",
                routing=routing,
            )
            logger.info(f"Saved schema {table!r} -> gitcrud {_class_directory()}")
            return meta_schema

        yaml_path = self._yaml_path(table=meta_schema.path)
        yaml_path.parent.mkdir(parents=True, exist_ok=True)

        yaml_dump(data=config_data, dest=yaml_path)

        # Refresh cache
        self._store._refresh_all()

        logger.info(f"Saved schema {table!r} -> {yaml_path!r}")
        return meta_schema

    def read_document(self, path: str) -> dict[str, Any] | None:
        """A copy of the stored YAML document at *path*, or None when there is none.

        The deploy repo is read first, then the schemas tree. Fixtures are not consulted.
        """
        doc = self._deploy_doc(path)
        if doc is None:
            yaml_path = self._yaml_path(path)
            if not yaml_path.is_file():
                return None
            loaded = yaml_load(yaml_path)
            if not isinstance(loaded, dict):
                raise SchemaValidationError(f"Schema YAML must be a mapping: {path!r}")
            doc = loaded
        return copy.deepcopy(doc)

    def write_document(
        self,
        path: str,
        doc: dict[str, Any],
        *,
        message: str,
        created_by: str | None = None,
        routing: ReviewRouting | None = None,
    ) -> MetaSchema:
        """Write one YAML document at *path* and return it parsed, as it reads once merged.

        The document is stored as given, so keys the model does not carry survive.
        ``routing`` records the write when it went to a review branch instead of main.

        Raises:
            SchemaValidationError: The document is not a valid meta schema.
        """
        canonical = canonical_schema_path(path)
        try:
            saved = self._parse_meta_schema(canonical, doc)
        except ValidationError as exc:
            raise SchemaValidationError(f"Invalid schema definition: {exc}") from exc
        saved.path = canonical

        if self._crud is not None:
            self._governed_put(
                canonical, doc, created_by=created_by, message=message, routing=routing
            )
            logger.info(f"Wrote schema {canonical!r} -> gitcrud {_class_directory()}")
            return saved

        yaml_path = self._yaml_path(canonical)
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_dump(doc, yaml_path)
        self._store._refresh_all()
        logger.info(f"Wrote schema {canonical!r} -> {yaml_path}")
        return saved

    def edit_schema(
        self,
        path: str,
        edit: Callable[[dict[str, Any]], None],
        *,
        message: str,
        created_by: str | None = None,
        routing: ReviewRouting | None = None,
    ) -> MetaSchema:
        """Apply *edit* to the stored document at *path* and write the result.

        A custom schema only the schemas tree holds is written to the deploy repo when
        gitops is on, where the copy then shadows it.

        Raises:
            SchemaNotFoundError: Neither the deploy repo nor the tree holds the schema.
            SchemaValidationError: The edited document is not a valid meta schema, or
                gitops is on and the schema is core.
        """
        doc = self.read_document(path)
        if doc is None:
            raise SchemaNotFoundError(f"Schema not found: {path!r}")
        if self._crud is not None and doc.get("resource_type") == "core":
            # A deploy-repo copy would shadow the shipped document, so later releases would never reach it.
            raise SchemaValidationError(
                f"Schema {path!r} is a core schema, which is shipped and changes through a "
                "release. Promote a field to fork it."
            )
        edit(doc)
        return self.write_document(
            path, doc, message=message, created_by=created_by, routing=routing
        )

    def save_fixture(self, meta_schema: MetaSchema) -> MetaSchema:
        """Write one schema into the fixtures directory. Never touches the schema tree or git.

        The fixtures directory is the e2e scratch copy. A missing directory, a
        directory inside the schema tree, or a directory git would record is
        refused before any file is created.
        """
        self._assert_fixtures_isolated()
        if not (meta_schema.path):
            raise SchemaValidationError("'MetaSchema.path' is required when saving")
        yaml_path = self._fixture_yaml(meta_schema.path)
        if yaml_path is None:
            raise SchemaError(
                "schema fixtures directory is not configured; refusing to write into the schema tree"
            )
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_dump(data=meta_schema.to_yaml_dict(), dest=yaml_path)
        logger.info(f"Saved schema fixture {meta_schema.path!r} -> {yaml_path!r}")
        return meta_schema

    def get_schema(self, path: str) -> MetaSchema:
        """Get a meta schema."""
        table = self._table_name(path)
        config_data = self._config(table)
        if config_data is None:
            desc = f"{path}"
            raise SchemaNotFoundError(f"Schema not found: {desc!r}")

        schema = MetaSchema.model_validate(config_data)
        schema.path = path
        return schema

    def _delete_deployed(
        self,
        path: str,
        *,
        removed_fixture: bool,
        created_by: str | None,
        routing: ReviewRouting | None,
    ) -> None:
        """Delete a schema from the deploy repo, where a production+team delete goes to review.

        Raises:
            SchemaValidationError: Only the read-only schemas tree holds the schema.
        """
        from dfe_engine.gitcrud.engine import ResourceNotFoundError

        key = canonical_schema_path(path)
        try:
            self._governed_write(
                path,
                lambda crud, name, actor, msg, branch: crud.delete(
                    META_CLASS, name, actor=actor, message=msg, branch=branch
                ),
                created_by=created_by,
                message=f"schema: delete {key}",
                routing=routing,
            )
        except ResourceNotFoundError:
            if self._store.get(self._table_name(path)) is not None:
                raise SchemaValidationError(
                    f"Schema {key!r} is held in the schemas tree, which is read-only when gitops is on"
                ) from None
            if not removed_fixture:
                logger.warning(f"Schema does not exist: {key!r}")
            return
        logger.info(f"Deleted schema {key!r} from gitcrud {_class_directory()}")

    def delete_schema(
        self,
        path: str,
        *,
        created_by: str | None = None,
        routing: ReviewRouting | None = None,
    ) -> None:
        """Delete a meta schema.

        ``routing`` records the delete when it went to a review branch instead of main.
        """
        table = self._table_name(path)
        removed_fixture = self.delete_fixture(path)
        if self._crud is not None:
            self._delete_deployed(
                path, removed_fixture=removed_fixture, created_by=created_by, routing=routing
            )
            return
        yaml_path = self._yaml_path(path)

        if not (yaml_path.exists()):
            if not removed_fixture:
                logger.warning(f"Schema file does not exist: {yaml_path!r}")
            return
        yaml_path.unlink(missing_ok=True)

        with self._store._lock:
            self._store._cache.pop(table, None)

        logger.info(f"Deleted schema {table!r}")

    def find_schema_at_location(self, schema_path: str) -> str | None:
        """Return an existing table key that occupies the same path location, if any.

        Uses DirectoryConfigStore table keys only (no YAML parsing). Registry keys
        match on-disk layout. ``MetaSchema.path`` is not persisted in YAML files.
        """
        parent, name = _schema_location(schema_path)
        for table in self._known_tables():
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
        for table in self._known_tables():
            if path and table != path:
                continue

            config_data = self._config(table)
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
            yaml_path = self._yaml_path(schema_rel)
            deployed = self._deploy_yaml(schema_rel)
            if deployed is not None and deployed.is_file():
                yaml_path = deployed
            elif not yaml_path.is_file():
                fixture = self._fixture_yaml(schema_rel)
                if fixture is not None:
                    yaml_path = fixture
            try:
                stat = yaml_path.stat()
                updated_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            except OSError:
                updated_at = ""

            # 'current' always names a real version. Fall back to the first if it ever doesn't.
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
    # Change Callbacks
    # -----------------------------------------------------------------

    def on_change(self, path: str, callback: Any) -> None:
        """Register a callback for when a schema changes."""
        table = self._table_name(path)
        self._store.on_change(table, callback)

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def close(self) -> None:
        """Stop background refresh and cleanup."""
        self._store.stop()
