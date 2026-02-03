import pandas as pd

from dfe_engine.schemas.custom_exceptions import SchemaError, SchemaBuilderWarning, SchemaBuilderJSONWarning
from hs_pylib import logger
from importlib import resources
from pathlib import Path


class SchemaUtils:

    RESOURCES_PACKAGE_PATH = "dfe_engine.resources"
    TYPE_MAPS_FILE_NAME = "type_maps.csv"
    TYPE_MAPS_PATH = "common"

    def __init__(
        self,
        type_maps_version: str = None,
        use_json_feature: bool = False
    ):
        resources_path = resources.files(self.RESOURCES_PACKAGE_PATH)
        
        if (type_maps_version):
            type_maps_version = type_maps_version.replace(".", "_")
        else:
            type_maps_version = self._get_latest_version_in_path(Path(resources_path) / self.TYPE_MAPS_PATH)

        self.type_maps_path = Path(resources_path) / self.TYPE_MAPS_PATH / type_maps_version / self.TYPE_MAPS_FILE_NAME
        self.use_json_feature = use_json_feature


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

            try:
                if ("json" in df["type"].values and not(self.use_json_feature)):
                    raise SchemaBuilderJSONWarning("The 'use_json_feature' is set to False. Continuing using the string mapping for the JSON type...")
            
            except SchemaBuilderWarning:
                df["json"] = df["string"]

        except Exception:
            raise
                
        return df