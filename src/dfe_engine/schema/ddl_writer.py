"""DDL File Writer — generates reference DDL SQL files for dfe-schemas.

Produces standalone, runnable SQL files from schema YAML definitions.
QoL tool for devs and sysops who want to see actual ClickHouse table
structures without running the engine.

Usage:
    from dfe_engine.schema.ddl_writer import DDLFileWriter

    writer = DDLFileWriter()
    written = writer.write_all(Path("schemas/ddl"))
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..source.type_registry import TypeRegistry
from .engine_resolver import EngineResolver
from .schema_ddl import DDLConfig, DDLGenerator, TableSpec
from .schema_loader import SchemaLoader, _resolve_profiles_dir, _resolve_schemas_root
from .table_loader import load_table_config

_PROFILES = ("timeseries", "minimal", "passthrough")
_DEFAULT_PROFILE = "timeseries"


class DDLFileWriter:
    """Generates and writes reference DDL SQL files.

    Orchestrates SchemaLoader and DDLGenerator to produce standalone ``.sql`` files for each known table structure.
    """

    def __init__(
        self,
        registry: TypeRegistry | None = None,
        *,
        topology: str = "single",
        resolver: EngineResolver | None = None,
        database: str = "{db}",
    ) -> None:
        """Initialise the writer.

        Args:
            registry: Type registry for primitive -> ClickHouse type resolution.
            topology: "single" -> <engine>(); "replicated" -> Replicated<engine>.
                     Ignored when *resolver* is given. Note this alone never
                     yields ON CLUSTER - only sensing does.
            resolver: Engine resolver to use instead of the static *topology*.
                     Build one with a live client (see ``EngineResolver``) so the
                     engine is sensed from the target server; that is the only
                     path that emits ON CLUSTER, which a real multi-node cluster
                     needs.
            database: Target database. Defaults to the ``{db}`` placeholder, which
                     is what the reference-SQL output wants (the caller substitutes
                     it). A LIVE caller must pass the REAL name: sensing keys on it
                     to read the database's engine, and "{db}" matches no database,
                     so a Replicated/Shared target would be misread as a plain one
                     and wrongly get ON CLUSTER (it replicates on its own).
        """
        self._registry = registry or TypeRegistry.default()
        self._ddl_gen = DDLGenerator(self._registry, resolver=resolver)
        self._topology = topology
        self._database = database

    @staticmethod
    def _profile_version(profile_name: str) -> str:
        """Get the current version of a profile from its YAML metadata."""
        try:
            profile_path = _resolve_profiles_dir() / f"{profile_name}.yaml"
            meta = SchemaLoader.load_version_metadata(profile_path)
            return meta.get("current", "1.0.0")
        except Exception:
            return "1.0.0"

    @staticmethod
    def _resolve_hunt_schema_path(filename: str, schemas_root: Path | None = None) -> Path:
        """Resolve a file under the dfe-schemas ``hunts/`` directory.

        The unresolved case raises here rather than building a path from None,
        which crashed the schema Job with a TypeError instead of saying what was
        missing.
        """
        schemas_root = schemas_root or _resolve_schemas_root()
        if schemas_root is None:
            raise FileNotFoundError(
                f"hunts/{filename} was not found: no dfe-schemas tree resolved. Set "
                "DFE_SCHEMAS_DIR, or install the dfe-schemas package"
            )
        candidate = schemas_root / "hunts" / filename
        if candidate.exists():
            return candidate
        raise FileNotFoundError(
            f"{str(candidate)!r} was not found. Ensure the dfe-schemas package is installed"
        )

    @staticmethod
    def _resolve_hunt_detection_checkpoint_path(schemas_root: Path | None = None) -> Path:
        """Resolve the path to hunts/detection_checkpoint.yaml."""
        return DDLFileWriter._resolve_hunt_schema_path("detection_checkpoint.yaml", schemas_root)

    @staticmethod
    def _resolve_hunt_results_path(schemas_root: Path | None = None) -> Path:
        """Resolve the path to hunts/results.yaml."""
        return DDLFileWriter._resolve_hunt_schema_path("results.yaml", schemas_root)

    def _core_config(self, ref: str, **overrides: Any) -> DDLConfig:
        """A core table's declared config from dfe-schemas, with call-site fields set.

        Retention and partitioning are declared in dfe-schemas so one definition
        serves the engine, the ArgoCD Job and the docker one-shot alike.
        """
        return replace(load_table_config(ref, self._database), **overrides)

    def _profile_table_spec(
        self,
        table_name: str,
        profile_name: str,
        profile_version: str | None = None,
        description: str | None = None,
        config_ref: str | None = None,
    ) -> TableSpec:
        """Describe a profile-only table: its columns and its DDL config."""
        profile_version = profile_version or self._profile_version(profile_name)

        columns = SchemaLoader.load_profile(
            profile_name=profile_name, profile_version=profile_version
        )
        overrides: dict[str, Any] = {
            "profile_name": profile_name,
            "profile_version": profile_version,
            "description": description,
            "topology": self._topology,
        }
        config = (
            self._core_config(config_ref, **overrides)
            if config_ref
            else DDLConfig(db=self._database, **overrides)
        )
        return TableSpec(name=table_name, columns=columns, config=config)

    def _render(self, spec: TableSpec) -> str:
        """Render a spec's CREATE TABLE."""
        return self._ddl_gen.generate_create_table(
            table_name=spec.name, columns=spec.columns, config=spec.config, generated_time=None
        )

    def default_table_spec(
        self, profile_name: str = _DEFAULT_PROFILE, profile_version: str | None = None
    ) -> TableSpec:
        """Describe the default ingestion table (profile columns only)."""
        return self._profile_table_spec(
            table_name="default",
            profile_name=profile_name,
            profile_version=profile_version,
            description="Default ingestion table (profile columns only)",
            config_ref="tables/core/default",
        )

    def generate_default_table(
        self, profile_name: str = _DEFAULT_PROFILE, profile_version: str | None = None
    ) -> str:
        """Generate DDL for the default ingestion table (profile columns only)."""
        return self._render(
            self.default_table_spec(profile_name=profile_name, profile_version=profile_version)
        )

    def generate_profile_table(self, profile_name: str, profile_version: str | None = None) -> str:
        """Generate DDL for a profile-only reference table."""
        return self._render(
            self._profile_table_spec(
                table_name=f"_{profile_name}_profile",
                profile_name=profile_name,
                profile_version=profile_version,
                description=f"Reference DDL for the {profile_name} common header profile",
            )
        )

    def detection_checkpoint_table_spec(
        self, detection_checkpoint_version: str | None = None, schemas_root_path: Path | None = None
    ) -> TableSpec:
        """Describe the hunt detection checkpoint table (no profile)."""
        detection_checkpoint_path = self._resolve_hunt_detection_checkpoint_path(
            schemas_root=schemas_root_path
        )
        detection_checkpoint_columns = SchemaLoader.load_columns(
            source=detection_checkpoint_path, version=detection_checkpoint_version
        )

        config = self._core_config(
            "tables/core/detection_checkpoint",
            schema_version=detection_checkpoint_version,
            description="Hunt execution checkpoint tracking",
            topology=self._topology,
        )
        return TableSpec(
            name="detection_checkpoint", columns=detection_checkpoint_columns, config=config
        )

    def generate_detection_checkpoint_table(
        self, detection_checkpoint_version: str | None = None, schemas_root_path: Path | None = None
    ) -> str:
        """Generate DDL for the hunt detection checkpoint table (no profile)."""
        return self._render(
            self.detection_checkpoint_table_spec(
                detection_checkpoint_version=detection_checkpoint_version,
                schemas_root_path=schemas_root_path,
            )
        )

    def detection_table_spec(
        self,
        profile_name: str = _DEFAULT_PROFILE,
        profile_version: str | None = None,
        hunt_results_version: str | None = None,
        schemas_root_path: Path | None = None,
    ) -> TableSpec:
        """Describe the hunt detection table.

        The table is ``detection`` and lives in the hunts database; the columns
        come from ``hunts/results.yaml`` composed onto the common header, minus
        whatever that schema's ``profile_exclude`` drops.
        """
        # Resolve so the table comment records the profile version it was built
        # from, the same as every other profile-composed table.
        profile_version = profile_version or self._profile_version(profile_name)

        hunt_results_path = self._resolve_hunt_results_path(schemas_root_path)
        hunt_results_columns = SchemaLoader.load_columns(
            hunt_results_path, version=hunt_results_version
        )
        profile_columns = SchemaLoader.load_profile(
            profile_name=profile_name, profile_version=profile_version
        )
        all_columns = SchemaLoader.compose(
            profile_columns=profile_columns,
            source_columns=hunt_results_columns,
            exclude=SchemaLoader.load_profile_exclude(hunt_results_path, hunt_results_version),
        )

        config = self._core_config(
            "tables/core/detection",
            profile_name=profile_name,
            profile_version=profile_version,
            schema_version=hunt_results_version,
            description="Hunt detection results (profile and hunts.results columns)",
            topology=self._topology,
        )
        return TableSpec(name="detection", columns=all_columns, config=config)

    def generate_detection_table(
        self,
        profile_name: str = _DEFAULT_PROFILE,
        profile_version: str | None = None,
        hunt_results_version: str | None = None,
        schemas_root_path: Path | None = None,
    ) -> str:
        """Generate DDL for the hunt detection table."""
        return self._render(
            self.detection_table_spec(
                profile_name=profile_name,
                profile_version=profile_version,
                hunt_results_version=hunt_results_version,
                schemas_root_path=schemas_root_path,
            )
        )

    def generate_all(self, schemas_root_path: Path | None = None) -> dict[str, Any]:
        """Generate all DDL files as a dict of {relative_path: sql_content}."""
        files: dict[str, Any] = {}

        files["default"] = {}
        files["profiles"] = {}
        for profile_name in _PROFILES:
            files["default"][profile_name] = {}
            files["profiles"][profile_name] = {}
            profile_path = _resolve_profiles_dir() / f"{profile_name}.yaml"
            profile_versions = SchemaLoader.load_version_metadata(profile_path)["versions"].keys()
            for profile_version in profile_versions:
                files["default"][profile_name][profile_version] = {}
                files["default"][profile_name][profile_version]["default.sql"] = (
                    self.generate_default_table(
                        profile_name=profile_name, profile_version=profile_version
                    )
                )
                files["profiles"][profile_name][profile_version] = {}
                files["profiles"][profile_name][profile_version][f"{profile_name}.sql"] = (
                    self.generate_profile_table(
                        profile_name=profile_name, profile_version=profile_version
                    )
                )

        files["detection_checkpoint"] = {}
        detection_checkpoint_path = self._resolve_hunt_detection_checkpoint_path(schemas_root_path)
        detection_checkpoint_versions = SchemaLoader.load_version_metadata(
            detection_checkpoint_path
        )["versions"].keys()
        for detection_checkpoint_version in detection_checkpoint_versions:
            files["detection_checkpoint"][detection_checkpoint_version] = {}
            files["detection_checkpoint"][detection_checkpoint_version][
                "detection_checkpoint.sql"
            ] = self.generate_detection_checkpoint_table(
                detection_checkpoint_version=detection_checkpoint_version
            )

        files["detection"] = {}
        hunt_results_path = self._resolve_hunt_results_path(schemas_root_path)
        hunt_results_versions = SchemaLoader.load_version_metadata(hunt_results_path)[
            "versions"
        ].keys()
        for hunt_results_version in hunt_results_versions:
            files["detection"][hunt_results_version] = {}
            for profile_name in _PROFILES:
                files["detection"][hunt_results_version][profile_name] = {}
                profile_versions = SchemaLoader.load_version_metadata(profile_path)[
                    "versions"
                ].keys()
                for profile_version in profile_versions:
                    files["detection"][hunt_results_version][profile_name][profile_version] = {}
                    files["detection"][hunt_results_version][profile_name][profile_version][
                        "detection.sql"
                    ] = self.generate_detection_table(
                        profile_name=profile_name,
                        profile_version=profile_version,
                        hunt_results_version=hunt_results_version,
                    )

        return files

    def write_all(self, output_dir: str | Path) -> list[Path]:
        """Write all DDL files to the output directory.

        Creates directories as needed. Returns list of paths written.
        """

        def _write_dict_files(file_dict: dict[str, Any], base_dir):
            written: list[Path] = []
            base = Path(base_dir)

            for key, value in file_dict.items():
                path = base / key
                if isinstance(value, dict):
                    path.mkdir(parents=True, exist_ok=True)
                    written.extend(_write_dict_files(value, path))
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value)
                    written.append(path)

            return written

        return _write_dict_files(self.generate_all(), base_dir=output_dir)
