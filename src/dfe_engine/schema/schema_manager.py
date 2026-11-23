"""Schema version management -- write operations for versioned schema YAML files.

Complements SchemaLoader (read-only) with functions that edit a schema document
in memory, which ``SchemaRegistry.edit_schema`` writes where the deployment
stores its schemas.

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
    from dfe_engine.schema.schema_manager import add_version_in, fork_document

    # Add a new version to a schema document
    add_version_in(
        document, "1.1.0",
        columns=[{"name": "geo_country", "type": "string"}, ...],
        origin="meta/syslog", type="addition", summary="Added geo columns",
    )

    # Fork one version of a schema document into a new custom schema
    fork = fork_document(document, source_version="1.0.0", summary="Forked", origin="meta/syslog")
"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from dfe_engine.schema.models import MetaSchema
from dfe_engine.schema.schema_loader import SchemaLoader, SchemaLoadError
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry, current_use_case


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

    Reads translate a retired name either way. Without this the old word rides
    forward into every version derived from the one that carried it.
    """
    rewritten = []
    for col in columns:
        declared = col.get("use_case")
        current = current_use_case(declared)
        rewritten.append({**col, "use_case": current} if current != declared else col)
    return rewritten


def _normalise_columns(
    columns: Sequence[dict[str, Any] | SchemaColumn],
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


# -- Document edits -------------------------------------------------
# In-memory edits that SchemaRegistry.edit_schema applies and writes.


def set_current_in(data: dict[str, Any], current: str, *, origin: str) -> None:
    """Point ``current`` at a version *data* defines. *origin* names it in errors."""
    versions = data.get("versions", {})
    if current not in versions:
        available = ", ".join(sorted(versions.keys())) or "(none)"
        raise SchemaVersionError(
            f"Version {current!r} not found in {origin}. Available: {available}"
        )
    data["current"] = current


def set_summary_in(data: dict[str, Any], version: str, summary: str, *, origin: str) -> None:
    """Replace the summary of one version entry in *data*."""
    versions = data.get("versions", {})
    if version not in versions:
        available = ", ".join(sorted(versions.keys())) or "(none)"
        raise SchemaVersionError(
            f"Version {version!r} not found in {origin}. Available: {available}"
        )

    entry = versions[version]
    if not isinstance(entry, dict):
        raise SchemaLoadError(f"Version entry for {version!r} must be a mapping: {origin}")
    entry["summary"] = summary


def delete_version_in(data: dict[str, Any], version: str, *, origin: str) -> None:
    """Remove a version entry from *data*, moving ``current`` off it when it was current.

    Raises:
        SchemaVersionError: The version is missing or is the last one.
        SchemaLoadError: The versions are not a mapping.
    """
    versions = data.get("versions", {})
    if not isinstance(versions, dict):
        raise SchemaLoadError(f"Schema versions must be a mapping: {origin}")

    if version not in versions:
        available = ", ".join(sorted(versions.keys())) or "(none)"
        raise SchemaVersionError(
            f"Version {version!r} not found in {origin}. Available: {available}"
        )

    if len(versions) == 1:
        raise SchemaVersionError(
            f"Cannot delete the last remaining version {version!r} in {origin}"
        )

    del versions[version]
    if data.get("current") == version:
        data["current"] = next(reversed(versions))


def add_version_in(
    data: dict[str, Any],
    new_version: str,
    columns: Sequence[dict[str, Any] | SchemaColumn],
    *,
    origin: str,
    type: str = "addition",
    summary: str = "",
    set_current: bool = True,
    validate: bool = True,
) -> None:
    """Add a version entry to *data*, leaving the existing entries untouched.

    Raises:
        SchemaVersionError: The version exists, there are no columns, or they do not validate.
    """
    versions = data.setdefault("versions", {})
    if new_version in versions:
        raise SchemaVersionError(f"Version {new_version!r} already exists in {origin}")

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


def _single_version_document(
    columns: list[dict[str, Any]], version: str, *, type: str, summary: str
) -> dict[str, Any]:
    """A schema document holding one version, current."""
    return {
        "current": version,
        "versions": {
            version: {
                "date": _today(),
                "type": type,
                "summary": summary,
                "columns": columns,
            }
        },
    }


def fork_document(
    data: dict[str, Any],
    *,
    source_version: str | None = None,
    summary: str,
    origin: str,
) -> dict[str, Any]:
    """A new single-version document carrying one version's columns from *data*.

    The fork keeps the version id it copies and is ``resource_type: custom``,
    so a core schema can be extended without being modified.

    Raises:
        SchemaVersionError: The version to copy is not defined.
        SchemaLoadError: The source holds no version tree.
    """
    versions = data.get("versions")
    if not isinstance(versions, dict):
        raise SchemaLoadError(f"Schema versions must be a mapping: {origin}")
    version = source_version or data.get("current")
    if not version or version not in versions:
        available = ", ".join(sorted(versions)) or "(none)"
        raise SchemaVersionError(
            f"Source version {version!r} not found in {origin}. Available: {available}"
        )
    raw_columns = versions[version].get("columns") if isinstance(versions[version], dict) else None
    if not isinstance(raw_columns, list):
        raise SchemaLoadError(f"Version {version!r} in {origin} must contain a 'columns' key")

    columns = _normalise_columns([SchemaColumn.model_validate(col) for col in raw_columns])
    fork = _single_version_document(columns, version, type="model", summary=summary)
    fork["resource_type"] = "custom"
    return fork


# -- SchemaManager --------------------------------------------------


class SchemaManager:
    """Column validation for meta schemas."""

    @staticmethod
    def validate_meta_schema_columns(meta: MetaSchema) -> None:
        """Validate every version's columns against TypeRegistry."""
        for version, ver in meta.versions.items():
            col_dicts = [col.to_yaml_dict() for col in ver.columns]
            try:
                _validate_columns(col_dicts)
            except SchemaVersionError as exc:
                raise SchemaVersionError(f"Version {version!r}: {exc}") from exc
