import pandas as pd

from dfe_engine.schemas.custom_exceptions import SchemaError
from hs_pylib import logger
from importlib import resources
from pathlib import Path
from typing import List


class SchemaUtils:

    RESOURCES_PACKAGE_PATH = "dfe_engine.resources"
    TYPE_MAPS_FILE_NAME = "type_maps.csv"
    TYPE_MAPS_PATH = "common"

    def __init__(
        self,
        type_maps_version: str = None
    ):
        resources_path = resources.files(self.RESOURCES_PACKAGE_PATH)
        
        if (type_maps_version):
            type_maps_version = type_maps_version.replace(".", "_")
        else:
            type_maps_version = self._get_latest_version_in_path(Path(resources_path) / self.TYPE_MAPS_PATH)

        self.type_maps_path = Path(resources_path) / self.TYPE_MAPS_PATH / type_maps_version / self.TYPE_MAPS_FILE_NAME


    @staticmethod
    def _get_latest_version_in_path(
        path: Path
    ) -> str:
        """
        Internal function that finds the latest version in a directory.
        """
        return max(dir.name for dir in path.iterdir() if dir.is_dir())


    @staticmethod
    def _csv_to_dataframe(
        csv_path: Path
    ) -> pd.DataFrame:
        """
        Internal function that loads a csv into a dataframe.
        """
        try:
            return pd.read_csv(csv_path)
        
        except Exception as e:
            raise SchemaError(e)


    def get_type_maps_dataframe(
        self
    ) -> pd.DataFrame:
        """
        Load a type map CSV file from a resource package and validate its structure.
        """
        try:
            df = self._csv_to_dataframe(self.type_maps_path)
            print(self.type_maps_path)

        except Exception as e:
            raise SchemaError(e)

        # type_columns = sorted(column_names)
        # cols_df = sorted(df.columns.tolist())

        # if cols_df != type_columns:
        #     error_msg = (
        #         f"Type CSV file is missing or has additional columns. "
        #         f"Should contain: {', '.join(type_columns)}; file {resource_path}."
        #     )
        #     raise CSVValidationError(error_msg)

        # df[dup_column] = df[dup_column].str.lower()

        # if not df[dup_column].is_unique:
        #     duplicate_values = df[df[dup_column].duplicated(keep=False)].sort_values(dup_column)
        #     error_msg = (
        #         f"Duplicate {dup_column} provided in file {resource_path}\n"
        #         + duplicate_values.to_markdown()
        #     )
        #     raise CSVValidationError(error_msg)

        return df