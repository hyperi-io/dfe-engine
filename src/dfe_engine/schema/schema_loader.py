"""Schema v2 YAML loader — replaces CSV-based loading in schema_util.py.

Loads schema definitions from YAML files into SchemaColumn models.
Handles meta_schema, derived_schema, additional_fields, and common
header profiles.

Supports per-file schema versioning via a **version tree**: each version
has its own complete column snapshot under ``versions.<ver>.columns``.
The ``current`` key names the default version.  Consumers can pin any
version; the engine returns the exact column snapshot for that version.

Profile resolution order (first match wins):
1. Explicit ``profiles_dir`` argument
2. ``DFE_SCHEMAS_DIR`` env var → ``{dir}/common-header/``
3. ``common-header/`` under the installed ``dfe-schemas`` package
4. ``common-header/`` under the image's schema seed directory

Usage:
    from dfe_engine.schema.schema_loader import SchemaLoader

    loader = SchemaLoader()
    columns = loader.load_meta_schema("/path/to/meta_schema.yaml")
    columns = loader.apply_derived_schema(columns, "/path/to/derived.yaml")
    columns = loader.apply_additional_fields(columns, "/path/to/additional.yaml")
    header = loader.load_profile("timeseries", profile_version="1.0.0")
    full = loader.compose(header, columns)
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import current_use_case
from dfe_engine.yaml_utils import yaml_load

# Subdirectories of a dfe-schemas tree, wherever that tree is resolved from.
_COMMON_HEADER_SUBDIR = "common-header"
_HUNTS_SUBDIR = "hunts"
REGISTRIES_SUBDIR = "registries"

# The apply manifest at the root of a dfe-schemas tree. Named here rather than
# imported so this module still loads when dfe-schemas is absent; a rename
# upstream fails test_the_real_package_maps_to_its_data_directory.
_MANIFEST_FILE = "manifest.yaml"

# The dfe-schemas distribution and the package-data directory its wheel
# force-includes the schema trees under.
_SCHEMAS_PACKAGE = "dfe_schemas"
_SCHEMAS_PACKAGE_DATA = "data"

# Where the container image ships the schemas. dfe_engine.bootstrap copies this
# tree into the runtime schemas dir and imports these from here.
DEFAULT_SCHEMAS_SEED_DIR = "/app/schemas-seed"
SEED_DIR_ENV_VAR = "DFE_SCHEMAS_SEED_DIR"


def _looks_like_schemas_root(candidate: Path) -> bool:
    """Whether *candidate* is a full dfe-schemas tree rather than a partial one.

    Requiring ``common-header/`` stops a partial tree such as ``config/schemas``
    -- or the empty directory the image seeds INTO -- shadowing a real checkout.
    The manifest is required on top of it because a schemas volume an earlier
    engine seeded carries ``common-header/`` but none of what came later: no
    ``topics/``, no ``views/``, no manifest. On the header alone such a tree
    shadowed the image's complete seed and the engine died on the first file it
    never carried. Completeness is the test, not a version -- the manifest is
    what every whole tree has and every partial one lacks.
    """
    return (
        candidate.is_dir()
        and (candidate / _COMMON_HEADER_SUBDIR).is_dir()
        and (candidate / _MANIFEST_FILE).is_file()
    )


def _resolve_package_schemas_root() -> Path | None:
    """Locate the schema trees inside the installed ``dfe-schemas`` package.

    ``importlib.resources`` finds the package wherever it is installed rather
    than guessing a path relative to this file. The wheel force-includes the
    trees under ``dfe_schemas/data/``, the same layout every resolver here
    expects at a schemas root; a source checkout on ``sys.path`` keeps them one
    level up instead, so both shapes are tried.

    Returns None when the package is absent or carries no trees.
    """
    try:
        package_dir = resources.files(_SCHEMAS_PACKAGE)
    except (ImportError, TypeError):
        return None

    packaged = Path(str(package_dir / _SCHEMAS_PACKAGE_DATA))
    checkout = Path(str(package_dir)).parent
    for candidate in (packaged, checkout):
        if _looks_like_schemas_root(candidate):
            return candidate
    return None


def _resolve_schemas_root() -> Path | None:
    """Resolve the dfe-schemas root directory.

    Order: ``DFE_SCHEMAS_DIR``, the installed ``dfe-schemas`` package, then the
    image's seed directory. The env var is first so a deployment's own tree
    wins. The seed is last and present at all because the container ships the
    schemas there: every process in the image needs to read them, while only the
    daemon runs the bootstrap that copies them out. Without it, `dfe-schema` in
    a Job had no schemas.

    Returns None when no schemas tree is found.
    """
    env_dir = os.getenv("DFE_SCHEMAS_DIR")
    if env_dir and _looks_like_schemas_root(Path(env_dir)):
        return Path(env_dir)

    packaged = _resolve_package_schemas_root()
    if packaged:
        return packaged

    seed_dir = Path(os.getenv(SEED_DIR_ENV_VAR, DEFAULT_SCHEMAS_SEED_DIR))
    if _looks_like_schemas_root(seed_dir):
        return seed_dir

    return None


def _resolve_subdir(name: str) -> Path:
    """Resolve one subdirectory of a dfe-schemas tree.

    The env var is consulted directly rather than through
    :func:`_resolve_schemas_root` so a directory carrying only the subdirectory
    asked for still answers, as it always has.

    Raises:
        SchemaLoadError: No schemas tree carries the subdirectory.
    """
    env_dir = os.getenv("DFE_SCHEMAS_DIR")
    if env_dir:
        candidate = Path(env_dir) / name
        if candidate.is_dir():
            return candidate

    root = _resolve_schemas_root()
    if root:
        candidate = root / name
        if candidate.is_dir():
            return candidate

    raise SchemaLoadError(
        f"No dfe-schemas tree carries {name!r}: set DFE_SCHEMAS_DIR or install dfe-schemas"
    )


def _resolve_hunts_schemas_dir() -> Path:
    """Resolve the hunts schemas directory: DFE_SCHEMAS_DIR, then the resolved schemas root."""
    return _resolve_subdir(_HUNTS_SUBDIR)


def _resolve_profiles_dir() -> Path:
    """Resolve the common-header profiles directory: DFE_SCHEMAS_DIR, then the resolved schemas root."""
    return _resolve_subdir(_COMMON_HEADER_SUBDIR)


def resolve_registry_path(name: str) -> Path:
    """``registries/<name>`` from the first dfe-schemas tree that carries it.

    Order: ``DFE_SCHEMAS_DIR``, the installed package, then the image seed. Each
    tree is checked for the entry itself, and a directory entry must hold at
    least one file, so a volume that predates the entry or carries an empty copy
    of it does not shadow the package.

    Raises:
        SchemaLoadError: No tree carries the entry.
    """
    roots = []
    env_dir = os.getenv("DFE_SCHEMAS_DIR")
    if env_dir:
        roots.append(Path(env_dir))
    packaged = _resolve_package_schemas_root()
    if packaged:
        roots.append(packaged)
    roots.append(Path(os.getenv(SEED_DIR_ENV_VAR, DEFAULT_SCHEMAS_SEED_DIR)))

    for root in roots:
        candidate = root / REGISTRIES_SUBDIR / name
        if (candidate.is_file()) or (candidate.is_dir() and any(candidate.rglob("*.yaml"))):
            return candidate

    searched = ", ".join(repr(str(root)) for root in roots)
    raise SchemaLoadError(
        f"No dfe-schemas tree carries {REGISTRIES_SUBDIR}/{name}; searched {searched}"
    )


_COMMON_HEADER_PREFIX = "common-header/"


def _reject_traversal(profile_name: str, normalized: str) -> None:
    """Refuse a profile ref that could climb out of the profiles directory.

    ``header.type`` is a free-form string written through the sources API and
    joined straight onto a directory, so a ``..`` segment reads any YAML the
    pod can see.
    """
    if any(part == ".." for part in normalized.split("/")):
        raise SchemaLoadError(f"Profile {profile_name!r} must not contain '..'")


_YAML_SUFFIXES = (".yaml", ".yml")


def _strip_yaml_suffix(name: str) -> str:
    """Drop a trailing ``.yaml``/``.yml`` so a stem can take ``.yaml`` once."""
    lower = name.lower()
    for suffix in _YAML_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _profile_file_stem(profile_name: str) -> str | None:
    """YAML stem for a profile ref, or None when it names no profile file.

    Empty, whitespace, a bare ``.yml``/``.yaml`` suffix, and the
    ``common-header`` directory (with or without a trailing slash) are not
    profiles — appending ``.yaml`` would look for ``.yaml`` or
    ``common-header.yaml``.
    """
    normalized = profile_name.replace("\\", "/").strip()
    _reject_traversal(profile_name, normalized.strip("/"))
    if normalized.startswith(_COMMON_HEADER_PREFIX):
        rest = normalized[len(_COMMON_HEADER_PREFIX) :]
    elif normalized.strip("/") == _COMMON_HEADER_SUBDIR:
        return None
    else:
        rest = normalized
    stem = _strip_yaml_suffix(rest.strip("/")).strip()
    return stem or None


def _profile_yaml_path(directory: Path, stem: str) -> Path:
    cleaned = _strip_yaml_suffix(stem).strip().strip("/")
    if not cleaned:
        raise SchemaLoadError("Profile name is empty")
    return directory / f"{cleaned}.yaml"


def _resolve_profile_yaml_path(
    profile_name: str,
    profiles_dir: str | Path | None = None,
) -> Path:
    """Map a profile ref (short name or ``common-header/…`` registry path) to a YAML file."""
    stem = _profile_file_stem(profile_name)
    if stem is None:
        raise SchemaLoadError(f"Profile {profile_name!r} is empty")

    if profiles_dir is not None:
        return _profile_yaml_path(Path(profiles_dir), stem)

    normalized = profile_name.replace("\\", "/").strip()
    # Traversal is already rejected in _profile_file_stem; keep the same
    # guard on this join path so a future edit cannot skip it.
    _reject_traversal(profile_name, normalized.strip("/"))

    def candidate_under(root: Path) -> Path:
        if normalized.startswith(_COMMON_HEADER_PREFIX) or "/" in normalized:
            parts = [p for p in normalized.split("/") if p]
            if len(parts) == 1:
                return _profile_yaml_path(root, parts[0])
            return _profile_yaml_path(root.joinpath(*parts[:-1]), parts[-1])
        return _profile_yaml_path(root / _COMMON_HEADER_SUBDIR, stem)

    # Same as ``_resolve_subdir``: DFE_SCHEMAS_DIR may be a partial tree
    # (common-header only, no manifest.yaml). ``_resolve_schemas_root``
    # would skip it and the packaged timeseries profile would win.
    env_dir = os.getenv("DFE_SCHEMAS_DIR")
    if env_dir:
        env_candidate = candidate_under(Path(env_dir))
        if env_candidate.is_file():
            return env_candidate

    schemas_root = _resolve_schemas_root()
    if schemas_root and (normalized.startswith(_COMMON_HEADER_PREFIX) or "/" in normalized):
        candidate = candidate_under(schemas_root)
        if candidate.exists():
            return candidate

    return _profile_yaml_path(_resolve_profiles_dir(), stem)


def resolve_schema_yaml_path(schemas_base: Path, path_str: str) -> Path:
    """Resolve a schema file reference under ``schemas_base``.

    Accepts legacy filenames (``meta.yaml``), explicit ``.yaml`` paths, and
    registry-style keys without a suffix (``meta/aws/cloudtrail``).
    Directories at the unsuffixed path are ignored so a nested schema
    ``meta/test/test.yaml`` is not shadowed by ``meta/test/test/``.
    """
    normalized = path_str.replace("\\", "/").strip("/")
    path = Path(normalized)
    if path.is_absolute():
        if path.is_file():
            return path
        if path.suffix not in (".yaml", ".yml"):
            return path.with_suffix(".yaml")
        return path

    candidates: list[Path] = [schemas_base / normalized]
    if not normalized.lower().endswith((".yaml", ".yml")):
        parts = [p for p in normalized.split("/") if p]
        if len(parts) == 1:
            candidates.append(schemas_base / f"{parts[0]}.yaml")
        elif parts:
            candidates.append(schemas_base.joinpath(*parts[:-1]) / f"{parts[-1]}.yaml")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


def is_shipped_schema(path: str | Path) -> bool:
    """Check whether a path is inside the shipped (read-only) schemas.

    Shipped schemas live in the resolved dfe-schemas tree or the installed
    package. Users should create their own files rather than modifying shipped
    ones.

    Returns True if the path resolves to a location inside the shipped
    schema directories.
    """
    resolved = Path(path).resolve()

    # Check the resolved schemas root (env var, package, or image seed)
    schemas_root = _resolve_schemas_root()
    if schemas_root and resolved.is_relative_to(schemas_root.resolve()):
        return True

    # The env var can point at a different tree; the installed package is
    # still shipped even when it is not the active root.
    packaged = _resolve_package_schemas_root()
    return bool(packaged and resolved.is_relative_to(packaged.resolve()))


def _extract_version_columns(data: dict[str, Any], version: str, path: Path) -> list[dict]:
    """Extract the column list for a specific version from a version tree.

    The version tree lives under ``versions.<ver>.columns``.
    Raises SchemaLoadError if the requested version is not found.
    """
    versions = data.get("versions", {})
    if version not in versions:
        available = ", ".join(sorted(versions.keys())) or "(none)"
        raise SchemaLoadError(
            f"Version {version!r} not found in {path}. Available versions: {available}"
        )
    ver_entry = versions[version]
    if not isinstance(ver_entry, dict) or "columns" not in ver_entry:
        raise SchemaLoadError(f"Version {version!r} in {path} must contain a 'columns' key")
    return ver_entry["columns"]


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
    def load_version_entry(
        source: str | Path,
        *,
        version: str | None = None,
        require_columns: bool = True,
    ) -> dict:
        """Load a schema YAML file's resolved version entry as a raw dict.

        Same file-layout and version resolution as :meth:`load_columns`.
        Returns the raw version mapping (``columns`` plus any sibling keys
        such as ``synthetic``); flat files are wrapped as ``{"columns": [...]}``.

        Args:
            source: Path to the schema YAML.
            version: Version to resolve. Defaults to the file's ``current``.
            require_columns: Reject a version entry that declares no columns. A
                config-only definition (a core table whose columns come from
                composition) passes ``False``.

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

        if not data:
            raise SchemaLoadError(f"Schema YAML is empty: {path}")

        # Resolve target version: explicit arg > file's current > None
        target_version = version or data.get("current")

        if target_version and "versions" in data:
            if require_columns:
                _extract_version_columns(data, target_version, path)
            elif target_version not in data.get("versions", {}):
                available = ", ".join(sorted(data.get("versions", {}))) or "(none)"
                raise SchemaLoadError(
                    f"Version {target_version!r} not found in {path}. Available: {available}"
                )
            return data["versions"][target_version]
        if "columns" in data:
            # Flat layout (unversioned or no version requested)
            return {"columns": data["columns"]}
        raise SchemaLoadError(f"Schema YAML must contain 'columns' or 'versions' key: {path}")

    @staticmethod
    def load_raw_columns(
        source: str | Path,
        *,
        version: str | None = None,
    ) -> list[dict]:
        """Load a schema YAML file's column list as raw dicts.

        Same file-layout and version resolution as :meth:`load_columns`, but
        returns the raw column mappings - for consumers that need keys the
        ``SchemaColumn`` model does not carry (e.g. synthetic data hints).

        Raises:
            SchemaLoadError: If file missing or invalid.
        """
        raw_columns = SchemaLoader.load_version_entry(source, version=version)["columns"]

        for i, col_data in enumerate(raw_columns):
            if not isinstance(col_data, dict):
                raise SchemaLoadError(
                    f"Column {i} in {source} must be a dict, got {type(col_data).__name__}"
                )
        return raw_columns

    @staticmethod
    def load_columns(
        source: str | Path,
        *,
        version: str | None = None,
    ) -> list[SchemaColumn]:
        """Load a schema YAML file into a list of SchemaColumn models.

        Supports two YAML layouts:

        **Version tree** (preferred for versioned schemas)::

            current: "1.0.0"
            versions:
              "1.0.0":
                date: "2026-01-15"
                type: model
                summary: "Initial schema"
                columns:
                  - name: _timestamp
                    type: datetime

        **Flat** (backward-compatible, unversioned)::

            columns:
              - name: _timestamp
                type: datetime

        When *version* is given, the column snapshot for that version is
        returned from the version tree.  When ``None``, the file's
        ``current`` marker selects the version.  Files without a
        ``versions`` key fall through to the flat ``columns`` list.

        Args:
            source: Path to YAML file.
            version: Target schema version (semver).  When ``None``,
                     uses the file's ``current`` marker.

        Returns:
            List of SchemaColumn models for the requested version.

        Raises:
            SchemaLoadError: If file missing or invalid.
        """
        raw_columns = SchemaLoader.load_raw_columns(source, version=version)

        columns = []
        for i, col_data in enumerate(raw_columns):
            try:
                columns.append(SchemaColumn.model_validate(col_data))
            except Exception as e:
                name = col_data.get("name", f"index {i}")
                raise SchemaLoadError(f"Invalid column {name!r} in {source}: {e}") from e

        return columns

    @staticmethod
    def load_version_metadata(source: str | Path) -> dict[str, Any]:
        """Load version metadata from a schema YAML file.

        Returns a dict with ``current`` (str) and ``versions`` (dict of
        version → metadata without columns).  Returns an empty dict for
        unversioned files.

        Raises:
            SchemaLoadError: If file missing or unparseable.
        """
        path = Path(source)
        if not path.exists():
            raise SchemaLoadError(f"Schema file not found: {path}")

        try:
            data = yaml_load(path)
        except Exception as e:
            raise SchemaLoadError(f"Failed to parse YAML: {path}: {e}") from e

        result: dict[str, Any] = {}
        if data and "current" in data:
            result["current"] = data["current"]
        if data and "versions" in data:
            # Return metadata only (strip columns to keep output lean)
            versions_meta: dict[str, Any] = {}
            for ver, entry in data["versions"].items():
                if isinstance(entry, dict):
                    versions_meta[ver] = {k: v for k, v in entry.items() if k != "columns"}
                else:
                    versions_meta[ver] = entry
            result["versions"] = versions_meta
        return result

    @staticmethod
    def load_profile_exclude(source: str | Path, version: str | None = None) -> list[str]:
        """Read a version's ``profile_exclude`` - header columns it drops.

        A schema composed onto a common header can declare columns of that
        header it does not want (``hunts/results.yaml`` drops ``_raw`` and
        ``_tags``). Returns an empty list when the field is absent.

        Raises:
            SchemaLoadError: If file missing or unparseable.
        """
        meta = SchemaLoader.load_version_metadata(source)
        versions = meta.get("versions") or {}
        entry = versions.get(version or meta.get("current")) or {}
        if not isinstance(entry, dict):
            return []
        return list(entry.get("profile_exclude") or [])

    @staticmethod
    def load_profile(
        profile_name: str,
        profiles_dir: str | Path | None = None,
        *,
        profile_version: str | None = None,
    ) -> list[SchemaColumn]:
        """Load a common header profile YAML.

        Profiles define the standard columns injected at the start of
        every schema (timeseries, minimal, passthrough).

        Resolution order (first match wins):
        1. Explicit ``profiles_dir`` argument
        2. ``DFE_SCHEMAS_DIR`` env var → ``{dir}/common-header/``
        3. ``common-header/`` under the installed ``dfe-schemas`` package
        4. ``common-header/`` under the image's schema seed directory

        Args:
            profile_name: Short profile name (e.g. ``timeseries``) or registry
                          path (e.g. ``common-header/minimal``).
            profiles_dir: Directory containing profile YAML files.
                          When provided, skips the resolution chain.
            version: Target schema version (semver).  When ``None``,
                     uses the file's ``current`` marker.

        Returns:
            List of SchemaColumn models for the profile at the given version.

        Raises:
            SchemaLoadError: If profile not found.
        """
        profile_path = _resolve_profile_yaml_path(profile_name, profiles_dir)

        if not profile_path.exists():
            raise SchemaLoadError(f"Profile {profile_name!r} not found at {profile_path}")

        return SchemaLoader.load_columns(profile_path, version=profile_version)

    # -----------------------------------------------------------------
    # Composition
    # -----------------------------------------------------------------

    @staticmethod
    def _derived_version_block(path: Path, version: str | None) -> dict[str, Any]:
        """Resolve a derived schema's version block, defaulting to ``current``."""
        try:
            data = yaml_load(path)
        except Exception as e:
            raise SchemaLoadError(f"Failed to parse YAML: {path}: {e}") from e
        if not data:
            raise SchemaLoadError(f"Derived schema is empty: {path}")

        versions = data.get("versions")
        if not isinstance(versions, dict) or not versions:
            raise SchemaLoadError(f"Derived schema {path} must contain a 'versions' key")

        wanted = version or data.get("current")
        if wanted is None:
            raise SchemaLoadError(f"Derived schema {path} declares no 'current' version")
        block = versions.get(str(wanted))
        if not isinstance(block, dict):
            raise SchemaLoadError(f"Version {wanted!r} not found in {path}")
        return block

    @staticmethod
    def _derived_select_entries(path: Path, version: str | None) -> list[dict[str, Any]]:
        """Read and shape-check a derived schema's ``select`` list."""
        block = SchemaLoader._derived_version_block(path, version)
        entries = block.get("select")
        if not isinstance(entries, list) or not entries:
            raise SchemaLoadError(f"Derived schema {path} must declare a non-empty 'select' list")

        checked: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or "name" not in entry:
                raise SchemaLoadError(f"Each 'select' entry in {path} needs a 'name': {entry!r}")
            extra = set(entry) - {"name", "index"}
            if extra:
                raise SchemaLoadError(
                    f"Derived schema {path} sets {sorted(extra)} on {entry['name']!r}. "
                    f"A derived schema selects columns and may override 'index'; every other "
                    f"field, 'expr' included, resolves from the base."
                )
            checked.append(entry)
        return checked

    @staticmethod
    def apply_derived_schema(
        base_columns: list[SchemaColumn],
        derived_source: str | Path,
        version: str | None = None,
    ) -> list[SchemaColumn]:
        """Narrow base columns to the subset a derived schema selects.

        The result is exactly the ``select`` list, in its order. A selected
        entry may override ``index`` (the column's index use case) and nothing
        else -- ``expr`` in particular resolves from the base, because it is the
        directive dfe-loader reads back out of the ClickHouse column comment.

        ``index: none`` keeps the column and drops its index.

        Raises:
            SchemaLoadError: The file is missing, the shape is wrong, or a
                selected name is absent from the base.
        """
        path = Path(derived_source)
        if not path.exists():
            raise SchemaLoadError(f"Derived schema not found: {path}")

        by_name = {column.name: column for column in base_columns}
        selected: list[SchemaColumn] = []

        for entry in SchemaLoader._derived_select_entries(path, version):
            name = entry["name"]
            base = by_name.get(name)
            if base is None:
                raise SchemaLoadError(
                    f"Derived schema {path} selects {name!r}, which its base does not define. "
                    f"A derived schema narrows its base and never adds to it."
                )
            if "index" not in entry:
                selected.append(base)
                continue
            index = entry["index"]
            # model_copy runs no validators, so the retired vocabulary is
            # translated here rather than on the way into the model.
            use_case = None if index == "none" else current_use_case(index)
            selected.append(base.model_copy(update={"use_case": use_case}))

        return selected

    @staticmethod
    def load_derived_capture(
        derived_source: str | Path, version: str | None = None
    ) -> dict[str, bool]:
        """Read a derived schema's catch-all switches.

        Stage 3: the table keeps its ``_json`` and ``_raw`` columns and its
        common header, and dfe-loader is told not to populate them. Both
        default to True, so omitting them keeps the safety net.
        """
        path = Path(derived_source)
        if not path.exists():
            raise SchemaLoadError(f"Derived schema not found: {path}")

        block = SchemaLoader._derived_version_block(path, version)
        capture: dict[str, bool] = {}
        for key in ("capture_json", "capture_raw"):
            value = block.get(key, True)
            if not isinstance(value, bool):
                raise SchemaLoadError(f"{key} in {path} must be true or false, got {value!r}")
            capture[key] = value
        return capture

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
        *,
        exclude: list[str] | None = None,
    ) -> list[SchemaColumn]:
        """Compose a full schema: profile header + source-specific columns.

        Profile columns come first. Source columns that duplicate a profile
        column name are dropped (profile wins).

        Args:
            profile_columns: Common header columns from the profile.
            source_columns: Source-specific schema columns.
            exclude: Header column names to drop (the schema's
                ``profile_exclude``). A name the profile does not have is
                ignored - a schema must compose onto any profile.

        Returns:
            Complete ordered list of SchemaColumn models.
        """
        if exclude:
            dropped = set(exclude)
            profile_columns = [col for col in profile_columns if col.name not in dropped]

        profile_names = {col.name for col in profile_columns}
        duplicates = [col.name for col in source_columns if col.name in profile_names]

        if duplicates:
            logger.warning(
                f"Source schema columns duplicate profile header, profile values used: {duplicates}"
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
                errors.append(f"Duplicate column name: {col.name!r}")
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
        ordered.sort(key=lambda c: c.order if c.order is not None else 0)
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
                logger.debug(f"Overriding column {override.name!r}")
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
