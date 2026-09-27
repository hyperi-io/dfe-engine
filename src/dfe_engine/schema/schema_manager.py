"""Schema version management -- write operations for versioned schema YAML files.

Complements SchemaLoader (read-only) with functions to create new versions
and clone schemas.  All operations are file-level -- the caller handles
git operations if needed.

The version tree format stores complete column snapshots per version::

    current: "1.0.0"
    versions:
      "1.0.0":
        date: "2026-01-15"
        type: model
        summary: "Initial schema"
        columns:
          - name: _timestamp
            type: datetime

Published version entries are **immutable** -- no function here modifies
an existing version's columns.  The only way to change a schema is to
add a new version.

Usage:
    from dfe_engine.schema.schema_manager import SchemaManager

    # Add a new version to an existing schema file
    SchemaManager.add_version(
        "meta/syslog.yaml", "1.1.0",
        columns=[{"name": "geo_country", "type": "string"}, ...],
        type="addition", summary="Added geo columns",
    )

    # Clone an existing version with modifications
    SchemaManager.clone_version(
        "meta/syslog.yaml", source_version="1.0.0", new_version="2.0.0",
        type="model", summary="Changed _raw type to string",
        column_modifications=[
            {"action": "update", "name": "_raw", "column": {"type": "string"}},
        ],
    )

    # Create a brand-new meta schema file
    SchemaManager.create_meta_schema(
        "meta/crowdstrike.yaml",
        columns=[{"name": "event_type", "type": "string"}, ...],
    )

    # Clone an entire schema file (for new source based on existing)
    SchemaManager.clone_meta_schema(
        "meta/syslog.yaml", "meta/syslog_custom.yaml",
    )
"""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from dfe_engine.schema.models import MetaSchema
from dfe_engine.schema.schema_loader import SchemaLoader, SchemaLoadError
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry, current_use_case
from dfe_engine.yaml_utils import yaml_dump, yaml_load


class SchemaVersionError(Exception):
    """Error related to schema version operations."""


_VERSION_TYPES = frozenset({"model", "addition", "revision"})
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SchemaVersionType = Literal["model", "addition", "revision"]


def next_version_for_type(current: str, version_type: SchemaVersionType) -> str:
    """Compute the next ``x.x.x`` semver from ``current`` and a version change type.

    ``model`` -> major bump, ``addition`` -> minor bump, ``revision`` -> patch bump.
    """
    if version_type not in _VERSION_TYPES:
        raise SchemaVersionError(
            f"Invalid version type {version_type!r}. Valid: model, addition, revision"
        )
    match = _SEMVER_RE.match(current.strip())
    if not match:
        raise SchemaVersionError(
            f"Current version {current!r} is not semver (expected x.x.x, e.g. 1.0.0)"
        )
    major, minor, patch = (int(match.group(i)) for i in range(1, 4))
    if version_type == "model":
        return f"{major + 1}.0.0"
    if version_type == "addition":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# -- Column normalisation -------------------------------------------


def _with_current_use_cases(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*columns* with a retired use case rewritten to its current name.

    Reads translate a retired name either way; without this the old word rides
    forward into every version derived from the one that carried it.
    """
    rewritten = []
    for col in columns:
        declared = col.get("use_case")
        current = current_use_case(declared)
        rewritten.append({**col, "use_case": current} if current != declared else col)
    return rewritten


def _normalise_columns(
    columns: list[dict[str, Any] | SchemaColumn],
) -> list[dict[str, Any]]:
    """Accept dicts or SchemaColumn models, return plain dicts for YAML."""
    result = []
    for col in columns:
        if isinstance(col, SchemaColumn):
            result.append(col.model_dump(mode="json", exclude_none=True))
        elif isinstance(col, dict):
            # Validate it parses as a SchemaColumn
            SchemaColumn.model_validate(col)
            result.append(col)
        else:
            raise SchemaVersionError(
                f"Column must be dict or SchemaColumn, got {type(col).__name__}"
            )
    return _with_current_use_cases(result)


def _validate_columns(columns: list[dict[str, Any]]) -> None:
    """Validate columns via TypeRegistry.  Raises SchemaVersionError on failure."""
    registry = TypeRegistry.default()
    parsed = [SchemaColumn.model_validate(c) for c in columns]
    errors = SchemaLoader.validate_columns(parsed, registry)
    if errors:
        raise SchemaVersionError("Column validation failed:\n  " + "\n  ".join(errors))


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _apply_modifications(
    columns: list[dict[str, Any]],
    modifications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply add/remove/update modifications to a column list.

    Each modification is a dict with an ``action`` key:

    - ``{"action": "add", "column": {...}}`` -- append a new column
    - ``{"action": "remove", "name": "col_name"}`` -- remove by name
    - ``{"action": "update", "name": "col_name", "column": {...}}`` -- merge
      the provided fields into the existing column dict
    """
    cols = copy.deepcopy(columns)
    for mod in modifications:
        action = mod.get("action")
        if action == "add":
            col_data = mod.get("column")
            if not col_data or "name" not in col_data:
                raise SchemaVersionError("'add' modification must include 'column' with 'name'")
            cols.append(col_data)

        elif action == "remove":
            name = mod.get("name")
            if not name:
                raise SchemaVersionError("'remove' modification must include 'name'")
            before = len(cols)
            cols = [c for c in cols if c.get("name") != name]
            if len(cols) == before:
                raise SchemaVersionError(f"Column {name!r} not found for removal")

        elif action == "update":
            name = mod.get("name")
            col_data = mod.get("column", {})
            if not name:
                raise SchemaVersionError("'update' modification must include 'name'")
            found = False
            for i, c in enumerate(cols):
                if c.get("name") == name:
                    cols[i] = {**c, **col_data}
                    found = True
                    break
            if not found:
                raise SchemaVersionError(f"Column {name!r} not found for update")

        else:
            raise SchemaVersionError(
                f"Unknown modification action: {action!r}. Valid: add, remove, update"
            )
    return cols


# -- SchemaManager --------------------------------------------------


class SchemaManager:
    """Write operations for versioned schema YAML files."""

    @staticmethod
    def set_current(path: str | Path, current: str) -> None:
        """Update the ``current`` marker without modifying version entries."""
        p = Path(path)
        if not p.exists():
            raise SchemaLoadError(f"Schema file not found: {p}")

        data = yaml_load(p)
        if not isinstance(data, dict):
            raise SchemaLoadError(f"Schema YAML must be a mapping: {p}")

        versions = data.get("versions", {})
        if current not in versions:
            available = ", ".join(sorted(versions.keys())) or "(none)"
            raise SchemaVersionError(
                f"Version {current!r} not found in {p}. Available: {available}"
            )

        data["current"] = current
        yaml_dump(data, p)

    @staticmethod
    def update_version_summary(path: str | Path, version: str, summary: str) -> None:
        """Update only the summary field on an existing version entry."""
        p = Path(path)
        if not p.exists():
            raise SchemaLoadError(f"Schema file not found: {p}")

        data = yaml_load(p)
        if not isinstance(data, dict):
            raise SchemaLoadError(f"Schema YAML must be a mapping: {p}")

        versions = data.get("versions", {})
        if version not in versions:
            available = ", ".join(sorted(versions.keys())) or "(none)"
            raise SchemaVersionError(
                f"Version {version!r} not found in {p}. Available: {available}"
            )

        entry = versions[version]
        if not isinstance(entry, dict):
            raise SchemaLoadError(f"Version entry for {version!r} must be a mapping: {p}")
        entry["summary"] = summary
        yaml_dump(data, p)

    @staticmethod
    def delete_version(path: str | Path, version: str) -> None:
        """Remove a version entry from an existing schema file.

        If the deleted version is ``current``, ``current`` is moved to the last
        remaining version (insertion order). The last remaining version cannot
        be deleted -- remove the schema instead.

        Raises:
            SchemaVersionError: If the version is missing or is the last version.
            SchemaLoadError: If the file is missing or malformed.
        """
        p = Path(path)
        if not p.exists():
            raise SchemaLoadError(f"Schema file not found: {p}")

        data = yaml_load(p)
        if not isinstance(data, dict):
            raise SchemaLoadError(f"Schema YAML must be a mapping: {p}")

        versions = data.get("versions", {})
        if not isinstance(versions, dict):
            raise SchemaLoadError(f"Schema versions must be a mapping: {p}")

        if version not in versions:
            available = ", ".join(sorted(versions.keys())) or "(none)"
            raise SchemaVersionError(
                f"Version {version!r} not found in {p}. Available: {available}"
            )

        if len(versions) == 1:
            raise SchemaVersionError(f"Cannot delete the last remaining version {version!r} in {p}")

        del versions[version]
        if data.get("current") == version:
            data["current"] = next(reversed(versions))
        yaml_dump(data, p)

    @staticmethod
    def validate_meta_schema_columns(meta: MetaSchema) -> None:
        """Validate every version's columns against TypeRegistry."""
        for version, ver in meta.versions.items():
            col_dicts = [col.to_yaml_dict() for col in ver.columns]
            try:
                _validate_columns(col_dicts)
            except SchemaVersionError as exc:
                raise SchemaVersionError(f"Version {version!r}: {exc}") from exc

    @staticmethod
    def add_version(
        path: str | Path,
        new_version: str,
        columns: Sequence[dict[str, Any] | SchemaColumn],
        *,
        type: str = "addition",
        summary: str = "",
        set_current: bool = True,
        validate: bool = True,
    ) -> None:
        """Add a new version entry to an existing schema file.

        Loads the file, validates columns, refuses if version already exists,
        and writes back.  Existing version entries are preserved untouched.

        Args:
            path: Path to the schema YAML file.
            new_version: Version string (semver, e.g. ``"1.1.0"``).
            columns: Column definitions (dicts or SchemaColumn models).
            type: Version type -- ``model``, ``addition``, or ``revision``.
            summary: Human-readable summary of the change.
            set_current: Whether to update the ``current`` marker.
            validate: Whether to validate columns against TypeRegistry.

        Raises:
            SchemaVersionError: If version exists or validation fails.
            SchemaLoadError: If the file is missing or malformed.
        """
        p = Path(path)
        if not p.exists():
            raise SchemaLoadError(f"Schema file not found: {p}")

        data = yaml_load(p)
        if not isinstance(data, dict):
            raise SchemaLoadError(f"Schema YAML must be a mapping: {p}")

        versions = data.setdefault("versions", {})
        if new_version in versions:
            raise SchemaVersionError(f"Version {new_version!r} already exists in {p}")

        col_dicts = _normalise_columns(columns)
        if not col_dicts:
            raise SchemaVersionError("columns must contain at least one column")
        if validate:
            _validate_columns(col_dicts)

        versions[new_version] = {
            "date": _today(),
            "type": type,
            "summary": summary,
            "columns": col_dicts,
        }

        if set_current:
            data["current"] = new_version

        yaml_dump(data, p)

    @staticmethod
    def clone_version(
        path: str | Path,
        new_version: str,
        *,
        source_version: str | None = None,
        type: str = "addition",
        summary: str = "",
        set_current: bool = True,
        column_modifications: list[dict[str, Any]] | None = None,
    ) -> list[SchemaColumn]:
        """Clone an existing version's columns into a new version.

        Reads the source version's columns, optionally applies modifications
        (add/remove/update), and creates a new version entry.

        Args:
            path: Path to the schema YAML file.
            new_version: Version string for the new entry.
            source_version: Version to clone from.  ``None`` uses ``current``.
            type: Version type.
            summary: Change summary.
            set_current: Whether to update the ``current`` marker.
            column_modifications: Optional list of column modifications.

        Returns:
            The new version's columns as SchemaColumn models.

        Raises:
            SchemaVersionError: If new version exists or source not found.
        """
        p = Path(path)
        if not p.exists():
            raise SchemaLoadError(f"Schema file not found: {p}")

        data = yaml_load(p)
        if not isinstance(data, dict):
            raise SchemaLoadError(f"Schema YAML must be a mapping: {p}")

        versions = data.get("versions", {})
        if new_version in versions:
            raise SchemaVersionError(f"Version {new_version!r} already exists in {p}")

        # Resolve source version
        src_ver = source_version or data.get("current")
        if not src_ver:
            raise SchemaVersionError(f"No source_version specified and no 'current' marker in {p}")
        if src_ver not in versions:
            available = ", ".join(sorted(versions.keys())) or "(none)"
            raise SchemaVersionError(
                f"Source version {src_ver!r} not found in {p}. Available: {available}"
            )

        # Deep-copy source columns
        source_cols = _with_current_use_cases(copy.deepcopy(versions[src_ver].get("columns", [])))

        # Apply modifications
        if column_modifications:
            source_cols = _apply_modifications(source_cols, column_modifications)

        # Validate
        _validate_columns(source_cols)

        # Add new version entry
        data.setdefault("versions", {})[new_version] = {
            "date": _today(),
            "type": type,
            "summary": summary,
            "columns": source_cols,
        }

        if set_current:
            data["current"] = new_version

        yaml_dump(data, p)

        return [SchemaColumn.model_validate(c) for c in source_cols]

    @staticmethod
    def create_meta_schema(
        path: str | Path,
        columns: Sequence[dict[str, Any] | SchemaColumn],
        *,
        initial_version: str = "1.0.0",
        type: str = "model",
        summary: str = "Initial schema",
        validate: bool = True,
    ) -> None:
        """Create a new schema YAML file with version tree format.

        Args:
            path: Path for the new file.  Must not already exist.
            columns: Column definitions.
            initial_version: First version string.
            type: Version type.
            summary: Version summary.
            validate: Whether to validate columns.

        Raises:
            SchemaVersionError: If path already exists or validation fails.
        """
        p = Path(path)
        if p.exists():
            raise SchemaVersionError(f"Schema file already exists: {p}")

        col_dicts = _normalise_columns(columns)
        if validate:
            _validate_columns(col_dicts)

        data = {
            "current": initial_version,
            "versions": {
                initial_version: {
                    "date": _today(),
                    "type": type,
                    "summary": summary,
                    "columns": col_dicts,
                }
            },
        }

        p.parent.mkdir(parents=True, exist_ok=True)
        yaml_dump(data, p)

    @staticmethod
    def clone_meta_schema(
        source_path: str | Path,
        dest_path: str | Path,
        *,
        source_version: str | None = None,
        new_version: str = "1.0.0",
        type: str = "model",
        summary: str = "",
    ) -> None:
        """Clone from an existing meta schema to create a new one.

        Reads column snapshot from the source file and creates a fresh
        single-version file at the destination.

        Args:
            source_path: Path to the source schema file.
            dest_path: Path for the new file.  Must not already exist.
            source_version: Version to clone from.  ``None`` uses ``current``.
            new_version: Version string for the new file.
            type: Version type.
            summary: Change summary.  Defaults to
                ``"Cloned from {source_path}"``.

        Raises:
            SchemaVersionError: If dest exists or source version not found.
        """
        src = Path(source_path)
        dst = Path(dest_path)

        if dst.exists():
            raise SchemaVersionError(f"Destination already exists: {dst}")
        if not src.exists():
            raise SchemaLoadError(f"Source schema not found: {src}")

        # Load source columns via SchemaLoader (handles version resolution)
        src_version = source_version
        if not src_version:
            meta = SchemaLoader.load_version_metadata(src)
            src_version = meta.get("current")

        columns = SchemaLoader.load_columns(src, version=src_version)

        if not summary:
            summary = f"Cloned from {src.name}"

        SchemaManager.create_meta_schema(
            dst,
            columns,
            initial_version=new_version,
            type=type,
            summary=summary,
            validate=False,  # Already valid from source
        )
