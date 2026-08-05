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

from pathlib import Path
from typing import Any

from ..source.type_registry import TypeRegistry
from .engine_resolver import EngineResolver
from .schema_ddl import DDLConfig, DDLGenerator
from .schema_loader import SchemaLoader, _resolve_profiles_dir, _resolve_schemas_root

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
    def _resolve_hunt_detection_checkpoint_path(schemas_root: Path | None = None) -> Path:
        """Resolve the path to hunts/detection_checkpoint.yaml."""
        schemas_root = schemas_root or _resolve_schemas_root()
        candidate = schemas_root / "hunts" / "detection_checkpoint.yaml"
        if schemas_root:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"{str(candidate)!r} was not found. Ensure the dfe-schemas submodule is checked out"
        )

    @staticmethod
    def _resolve_hunt_results_path(schemas_root: Path | None = None) -> Path:
        """Resolve the path to hunt/results.yaml."""
        schemas_root = schemas_root or _resolve_schemas_root()
        candidate = schemas_root / "hunts" / "results.yaml"
        if schemas_root:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"{str(candidate)!r} was not found. Ensure the dfe-schemas submodule is checked out"
        )

    def _profile_table_create(
        self,
        table_name: str,
        profile_name: str,
        profile_version: str | None = None,
        description: str | None = None,
    ) -> str:
        """Generate DDL for a profile-only reference table."""
        profile_version = profile_version or self._profile_version(profile_name)

        columns = SchemaLoader.load_profile(
            profile_name=profile_name, profile_version=profile_version
        )
        config = DDLConfig(
            db=self._database,
            profile_name=profile_name,
            profile_version=profile_version,
            description=description,
            topology=self._topology,
        )
        ddl = self._ddl_gen.generate_create_table(
            table_name=table_name, columns=columns, config=config, generated_time=None
        )
        return ddl

    def generate_default_table(
        self, profile_name: str = _DEFAULT_PROFILE, profile_version: str | None = None
    ) -> str:
        """Generate DDL for the default ingestion table (profile columns only)."""
        table_name = "default"
        return self._profile_table_create(
            table_name=table_name,
            profile_name=profile_name,
            profile_version=profile_version,
            description="Default ingestion table (profile columns only)",
        )

    def generate_profile_table(self, profile_name: str, profile_version: str | None = None) -> str:
        """Generate DDL for a profile-only reference table."""
        table_name = f"_{profile_name}_profile"
        return self._profile_table_create(
            table_name=table_name,
            profile_name=profile_name,
            profile_version=profile_version,
            description=f"Reference DDL for the {profile_name} common header profile",
        )

    def generate_detection_checkpoint_table(
        self, detection_checkpoint_version: str | None = None, schemas_root_path: Path | None = None
    ) -> str:
        """Generate DDL for the hunt detection checkpoint table (no profile)."""
        table_name = "detection_checkpoint"
        table_description = "Hunt execution checkpoint tracking"
        ttl_days = 365

        detection_checkpoint_path = self._resolve_hunt_detection_checkpoint_path(
            schemas_root=schemas_root_path
        )
        detection_checkpoint_columns = SchemaLoader.load_columns(
            source=detection_checkpoint_path, version=detection_checkpoint_version
        )

        config = DDLConfig(
            db=self._database,
            partition_column="query_checkpoint_time",
            partition_granularity="month",
            schema_version=detection_checkpoint_version,
            description=table_description,
            ttl_days=ttl_days,
            topology=self._topology,
        )
        return self._ddl_gen.generate_create_table(
            table_name=table_name,
            columns=detection_checkpoint_columns,
            config=config,
            generated_time=None,
        )

    def generate_hunt_results_table(
        self,
        profile_name: str = _DEFAULT_PROFILE,
        profile_version: str | None = None,
        hunt_results_version: str | None = None,
        schemas_root_path: Path | None = None,
    ) -> str:
        """Generate DDL for the hunt results table."""
        table_name = "hunt_results"
        table_description = "Hunt detection results (profile and hunts.results columns)"
        ttl_days = 365

        hunt_results_path = self._resolve_hunt_results_path(schemas_root_path)
        hunt_results_columns = SchemaLoader.load_columns(
            hunt_results_path, version=hunt_results_version
        )
        profile_columns = SchemaLoader.load_profile(
            profile_name=profile_name, profile_version=profile_version
        )
        all_columns = SchemaLoader.compose(
            profile_columns=profile_columns, source_columns=hunt_results_columns
        )

        config = DDLConfig(
            db=self._database,
            profile_name=profile_name,
            profile_version=profile_version,
            schema_version=hunt_results_version,
            description=table_description,
            ttl_days=ttl_days,
            topology=self._topology,
        )
        return self._ddl_gen.generate_create_table(
            table_name=table_name, columns=all_columns, config=config, generated_time=None
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

        files["hunt_results"] = {}
        hunt_results_path = self._resolve_hunt_results_path(schemas_root_path)
        hunt_results_versions = SchemaLoader.load_version_metadata(hunt_results_path)[
            "versions"
        ].keys()
        for hunt_results_version in hunt_results_versions:
            files["hunt_results"][hunt_results_version] = {}
            for profile_name in _PROFILES:
                files["hunt_results"][hunt_results_version][profile_name] = {}
                profile_versions = SchemaLoader.load_version_metadata(profile_path)[
                    "versions"
                ].keys()
                for profile_version in profile_versions:
                    files["hunt_results"][hunt_results_version][profile_name][profile_version] = {}
                    files["hunt_results"][hunt_results_version][profile_name][profile_version][
                        "hunt_results.sql"
                    ] = self.generate_hunt_results_table(
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
