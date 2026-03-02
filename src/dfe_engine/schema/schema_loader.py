"""Schema v2 YAML loader — replaces CSV-based loading in schema_util.py.

Loads schema definitions from YAML files into SchemaColumn models.
Handles meta_schema, derived_schema, additional_fields, and common
header profiles.

Profile resolution order (first match wins):
1. Explicit ``profiles_dir`` argument
2. ``DFE_SCHEMAS_DIR`` env var → ``{dir}/common-header/``
3. ``schemas/common-header/`` submodule (relative to project root)
4. Bundled ``schema/profiles/`` inside the package

Usage:
    from dfe_engine.schema.schema_loader import SchemaLoader

    loader = SchemaLoader()
    columns = loader.load_meta_schema("/path/to/meta_schema.yaml")
    columns = loader.apply_derived_schema(columns, "/path/to/derived.yaml")
    columns = loader.apply_additional_fields(columns, "/path/to/additional.yaml")
    header = loader.load_profile("timeseries")
    full = loader.compose(header, columns)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hyperi_pylib.logger import logger

from dfe_engine.source.models import SchemaColumn
from dfe_engine.yaml_utils import yaml_load

# Submodule location relative to project root.
_SUBMODULE_COMMON_HEADER = "schemas/common-header"
_SUBMODULE_ROOT = "schemas"

# Bundled profiles inside the package (fallback).
_BUNDLED_PROFILES_DIR = Path(__file__).parent / "profiles"


def _find_project_root() -> Path | None:
    """Walk up from this file to find the project root (contains pyproject.toml)."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / "pyproject.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _resolve_profiles_dir() -> Path:
    """Resolve the common-header profiles directory.

    Order: DFE_SCHEMAS_DIR env var → submodule → bundled.
    """
    # 1. Env var override
    env_dir = os.getenv("DFE_SCHEMAS_DIR")
    if env_dir:
        candidate = Path(env_dir) / "common-header"
        if candidate.is_dir():
            return candidate

    # 2. Submodule (relative to project root)
    root = _find_project_root()
    if root:
        candidate = root / _SUBMODULE_COMMON_HEADER
        if candidate.is_dir():
            return candidate

    # 3. Bundled fallback
    return _BUNDLED_PROFILES_DIR


def _resolve_schemas_root() -> Path | None:
    """Resolve the dfe-schemas root directory (submodule or env var).

    Returns None if only bundled profiles are available.
    """
    env_dir = os.getenv("DFE_SCHEMAS_DIR")
    if env_dir:
        candidate = Path(env_dir)
        if candidate.is_dir():
            return candidate

    root = _find_project_root()
    if root:
        candidate = root / _SUBMODULE_ROOT
        if candidate.is_dir():
            return candidate

    return None


def is_shipped_schema(path: str | Path) -> bool:
    """Check whether a path is inside the shipped (read-only) schemas.

    Shipped schemas live in the dfe-schemas submodule or the bundled
    profiles directory. Users should create their own files rather than
    modifying shipped ones.

    Returns True if the path resolves to a location inside the shipped
    schema directories.
    """
    resolved = Path(path).resolve()

    # Check submodule / env var root
    schemas_root = _resolve_schemas_root()
    if schemas_root and resolved.is_relative_to(schemas_root.resolve()):
        return True

    # Check bundled profiles
    if resolved.is_relative_to(_BUNDLED_PROFILES_DIR.resolve()):
        return True

    return False


class SchemaLoadError(Exception):
    """Error loading or validating a schema YAML file."""


class SchemaLoader:
    """Loads schema YAML files into lists of SchemaColumn models.

    Replaces the CSV-based loading in SchemaUtils (schema_util.py).
    Works with native Python dicts and SchemaColumn Pydantic models
    instead of pandas DataFrames.
    """

    # -----------------------------------------------------------------
    # Loading
    # -----------------------------------------------------------------

    @staticmethod
    def load_columns(source: str | Path) -> list[SchemaColumn]:
        """Load a schema YAML file into a list of SchemaColumn models.

        Expected YAML format:
            columns:
              - name: user_name
                type: string
                use_case: dimension
              - name: source_ip
                type: ip
                ...

        Args:
            source: Path to YAML file.

        Returns:
            List of SchemaColumn models.

        Raises:
            SchemaLoadError: If file missing or invalid.
        """
        path = Path(source)
        if not path.exists():
            raise SchemaLoadError(f"Schema file not found: {path}")

        try:
            data = yaml_load(path)
        except Exception as e:
            raise SchemaLoadError(f"Failed to parse YAML: {path}: {e}") from e

        if not data or "columns" not in data:
            raise SchemaLoadError(
                f"Schema YAML must contain a 'columns' key: {path}"
            )

        columns = []
        for i, col_data in enumerate(data["columns"]):
            if not isinstance(col_data, dict):
                raise SchemaLoadError(
                    f"Column {i} in {path} must be a dict, got {type(col_data).__name__}"
                )
            try:
                columns.append(SchemaColumn.model_validate(col_data))
            except Exception as e:
                name = col_data.get("name", f"index {i}")
                raise SchemaLoadError(
                    f"Invalid column '{name}' in {path}: {e}"
                ) from e

        return columns

    @staticmethod
    def load_profile(profile_name: str, profiles_dir: str | Path | None = None) -> list[SchemaColumn]:
        """Load a common header profile YAML.

        Profiles define the standard columns injected at the start of
        every schema (timeseries, minimal, passthrough).

        Resolution order (first match wins):
        1. Explicit ``profiles_dir`` argument
        2. ``DFE_SCHEMAS_DIR`` env var → ``{dir}/common-header/``
        3. ``schemas/common-header/`` submodule (relative to project root)
        4. Bundled ``schema/profiles/`` inside the package

        Args:
            profile_name: Profile name (e.g. 'timeseries').
            profiles_dir: Directory containing profile YAML files.
                          When provided, skips the resolution chain.

        Returns:
            List of SchemaColumn models for the profile.

        Raises:
            SchemaLoadError: If profile not found.
        """
        if profiles_dir:
            profile_path = Path(profiles_dir) / f"{profile_name}.yaml"
        else:
            resolved_dir = _resolve_profiles_dir()
            profile_path = resolved_dir / f"{profile_name}.yaml"

        if not profile_path.exists():
            raise SchemaLoadError(
                f"Profile '{profile_name}' not found at {profile_path}"
            )

        return SchemaLoader.load_columns(profile_path)

    # -----------------------------------------------------------------
    # Composition
    # -----------------------------------------------------------------

    @staticmethod
    def apply_derived_schema(
        base_columns: list[SchemaColumn],
        derived_source: str | Path,
    ) -> list[SchemaColumn]:
        """Apply a derived schema (overrides) to base columns.

        Derived schema columns override matching base columns by name.
        Only the fields specified in the derived column are overridden —
        unspecified fields keep their base values.

        Args:
            base_columns: Base schema columns.
            derived_source: Path to derived schema YAML.

        Returns:
            Updated list of SchemaColumn models.
        """
        path = Path(derived_source)
        if not path.exists():
            logger.warning(f"Derived schema not found, skipping: {path}")
            return base_columns

        derived_columns = SchemaLoader.load_columns(path)
        return SchemaLoader._merge_columns(base_columns, derived_columns)

    @staticmethod
    def apply_additional_fields(
        base_columns: list[SchemaColumn],
        additional_source: str | Path,
    ) -> list[SchemaColumn]:
        """Append additional fields to base columns.

        Additional fields are appended. If a column name already exists
        in the base, it is overridden (with warning).

        Args:
            base_columns: Base schema columns.
            additional_source: Path to additional fields YAML.

        Returns:
            Updated list of SchemaColumn models.
        """
        path = Path(additional_source)
        if not path.exists():
            logger.warning(f"Additional fields not found, skipping: {path}")
            return base_columns

        additional_columns = SchemaLoader.load_columns(path)
        return SchemaLoader._merge_columns(base_columns, additional_columns)

    @staticmethod
    def compose(
        profile_columns: list[SchemaColumn],
        source_columns: list[SchemaColumn],
    ) -> list[SchemaColumn]:
        """Compose a full schema: profile header + source-specific columns.

        Profile columns come first. Source columns that duplicate a profile
        column name are dropped (profile wins).

        Args:
            profile_columns: Common header columns from the profile.
            source_columns: Source-specific schema columns.

        Returns:
            Complete ordered list of SchemaColumn models.
        """
        profile_names = {col.name for col in profile_columns}
        duplicates = [col.name for col in source_columns if col.name in profile_names]

        if duplicates:
            logger.warning(
                f"Source schema columns duplicate profile header, "
                f"profile values used: {duplicates}"
            )

        unique_source = [col for col in source_columns if col.name not in profile_names]
        return list(profile_columns) + unique_source

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    @staticmethod
    def validate_columns(
        columns: list[SchemaColumn],
        registry: Any,
    ) -> list[str]:
        """Validate all columns against a TypeRegistry.

        Args:
            columns: Schema columns to validate.
            registry: TypeRegistry instance.

        Returns:
            List of error messages (empty if all valid).
        """
        errors: list[str] = []
        seen_names: set[str] = set()

        for col in columns:
            # Check for duplicate names
            normalized = col.name.replace(".", "_").replace("-", "_")
            if normalized in seen_names:
                errors.append(f"Duplicate column name: '{col.name}'")
            seen_names.add(normalized)

            # Validate against type registry
            errors.extend(col.validate_against_registry(registry))

        return errors

    @staticmethod
    def get_order_by_columns(columns: list[SchemaColumn]) -> list[str]:
        """Extract ORDER BY columns sorted by their order field.

        Returns column names where `order` is not None, sorted by order value.
        """
        ordered = [col for col in columns if col.order is not None]
        ordered.sort(key=lambda c: c.order)
        return [col.name for col in ordered]

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    @staticmethod
    def _merge_columns(
        base: list[SchemaColumn],
        overrides: list[SchemaColumn],
    ) -> list[SchemaColumn]:
        """Merge override columns into base, replacing by name.

        Override columns replace matching base columns entirely.
        New columns (not in base) are appended at the end.
        """
        base_map = {col.name: col for col in base}

        for override in overrides:
            if override.name in base_map:
                logger.debug(f"Overriding column '{override.name}'")
            base_map[override.name] = override

        # Preserve original order: base columns first (possibly overridden),
        # then new columns from overrides
        result = []
        seen = set()
        for col in base:
            result.append(base_map[col.name])
            seen.add(col.name)
        for override in overrides:
            if override.name not in seen:
                result.append(override)
                seen.add(override.name)

        return result
