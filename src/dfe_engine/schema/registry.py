"""Meta schema Registry - CRUD management for meta schema definitions.

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

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from scalo.config import DirectoryConfigStore
from scalo.logger import logger

from dfe_engine.schema.models import MetaSchema
from dfe_engine.schema.schema_loader import REGISTRIES_SUBDIR
from dfe_engine.yaml_utils import yaml_dump, yaml_load


class SchemaError(Exception):
    """Base exception for schema registry errors."""


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
    ) -> None:
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

    def _config(self, table: str) -> dict[str, Any] | None:
        """Schema document from the tree, falling back to a fixture of the same path."""
        data = self._store.get(table)
        if isinstance(data, dict):
            return data
        return self._fixture_data(table)

    def _known_tables(self) -> list[str]:
        """Tree tables plus fixture paths the tree does not already have."""
        tables = list(self._store.list_tables())
        have = set(tables)
        for table in self._fixture_tables():
            if table not in have:
                tables.append(table)
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
        config_data = self._store.get(table)
        if config_data is None:
            raise SchemaNotFoundError(f"Schema not found: '{path}'")
        current = config_data.get("current")
        if not isinstance(current, str) or not current.strip():
            raise SchemaValidationError(f"Schema '{path}' has no valid current version pointer")
        return current

    def schema_version_exists(self, path: str, version: str) -> bool:
        """Whether ``version`` is already present in on-disk YAML (raw store view)."""
        table = self._table_name(path)
        config_data = self._store.get(table)
        if not isinstance(config_data, dict):
            return False
        versions = config_data.get("versions")
        return isinstance(versions, dict) and version in versions

    def save_schema(self, meta_schema: MetaSchema | dict[str, Any]) -> MetaSchema:
        """Save a meta schema definition."""
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

        yaml_path = self._yaml_path(table=meta_schema.path)
        yaml_path.parent.mkdir(parents=True, exist_ok=True)

        yaml_dump(data=config_data, dest=yaml_path)

        # Refresh cache
        self._store._refresh_all()

        logger.info(f"Saved schema {table!r} -> {yaml_path!r}")
        return meta_schema

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

    def notify_schema_file_updated(self, path: str) -> MetaSchema:
        """Refresh cache after a direct on-disk schema YAML write."""
        table = self._table_name(path)
        yaml_path = self._yaml_path(path)
        if not yaml_path.exists():
            raise SchemaNotFoundError(f"Schema not found: {path!r}")

        self._store._refresh_all()
        logger.info(f"Schema file updated {table!r} -> {yaml_path}")
        return self.get_schema(path)

    def delete_schema(self, path: str) -> None:
        """Delete a meta schema."""
        table = self._table_name(path)
        removed_fixture = self.delete_fixture(path)
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
            if not yaml_path.is_file():
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
