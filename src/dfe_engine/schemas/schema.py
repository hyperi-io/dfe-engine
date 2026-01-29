from dfe_engine.schemas.custom_exceptions import SchemaFileNotFoundError
from hs_pylib import logger
from pathlib import Path
from typing import Optional


class Schema:

    DEFAULT_TTL = 90

    def __init__(
        self,
        name: str,
        derived_schema_directory: Path,
        derived_schema_version: str,
        derived_schemas_path: Path,
        meta_schema_name: str,
        meta_schema_version: str,
        meta_schemas_path: Path,
        ttl: Optional[int] = None
    ):
        derived_schema_version = derived_schema_version.replace(".", "_")
        meta_schema_version = meta_schema_version.replace(".", "_")
        
        self.info = {
            "name": name,
            "derived_schema": {
                "name": Path(derived_schema_directory).name,
                "version": derived_schema_version,
                "path": self._file_exists(derived_schemas_path / derived_schema_directory / derived_schema_version / f"{Path(derived_schema_directory).name}.csv")
            },
            "meta_schema": {
                "name": meta_schema_name,
                "version": meta_schema_version,
                "path": self._file_exists(meta_schemas_path / meta_schema_name / meta_schema_version / f"{meta_schema_name}.csv")
            },
            "ttl": ttl if (ttl is not None) else self.DEFAULT_TTL
        }


    def _file_exists(
        self,
        file_path: Path
    ) -> bool:
        """
        Internal function to check if a file exists.
        """
        logger.debug(f"Checking existence of file '{file_path}'...")
        if not(file_path.is_file()):
            raise SchemaFileNotFoundError(file_path)
        
        return (file_path.is_file())