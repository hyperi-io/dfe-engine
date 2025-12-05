import os

import httpx
from hs_lib.logger import logger
from ..config.config_loader import DFEConfigLoader
from .pipeline_builder import PipelineBuilder
from .pipeline_util import  merge_configs
from typing import Optional
from typing import List, Dict
from importlib import resources
from tabulate import tabulate
from zipfile import ZipFile
class PipelineBuilderController:
    @staticmethod
    def get_resource_path(logger: logging.Logger, package: str, resource_path: str):
        try:
            return resources.files(package) / resource_path
        except FileNotFoundError:
            logger.error(f"Resource does not exist: {package}/{resource_path}")
            return None
        
    @staticmethod
    def list_ingestion_templates(
        args_dfe_package_file_path: str,
        args_log_path: str,
        args_download: bool = False,
    ) -> Dict[str, List[str]]:
        """
        List all vector templates from the provided DFE package file.

        Parameters:
            args_dfe_package_file_path (str): The path to the dfe_package file.
            args_log_path (str): Path to the log directory.

        Returns:
            Dict[str, List[str]]: A dictionary with template types as keys and lists of template names as values.
        """
        logger = logger
        try:
            dfe_config_data = DFEConfigLoader.load_dfe_package(
                config_file_path=args_dfe_package_file_path
            )
        except FileNotFoundError as error:
            logger.error(f"Error: {error} reading package data using default values", exc_info=True)
            dfe_config_data = {
                "global_settings": {
                    "vector_files": {
                        "core": "./hs_artefacts/src/core_templates",
                        "custom": "./hs_artefacts/src/custom_templates",
                    }
                }
            }
        except Exception as error:
            logger.error(f"Error: {error} reading package data using default values", exc_info=True)
            return {}
        
        vector_files = dfe_config_data.get(
            "global_settings", {}
        ).get("vector_files", {})
        if not vector_files:
            logger.warning("No vector files found in the configuration.")
            return {}
        if args_download:
            # Download the vector templates if the flag is set
            PipelineBuilderController.download_templates(
                args_log_path=args_log_path,
                args_output=dfe_config_data["global_settings"]["vector_files"].get("core").split("/src/")[0],
                args_version="latest",
            )
            logger.info("Vector templates downloaded successfully.")
        # Call the new method to read vector templates
        formatted_templates = {"type": [], "templates": []}
        for key, val in vector_files.items():
            if key not in ["standard", "geoip"]:
                logger.info(f"Listing {key} templates: at {val}")
                # get the files in the directory that ends wih either .yml or .yaml
                if os.path.exists(val):
                    for file in os.listdir(val):
                        if file.endswith((".yml", ".yaml")):
                            formatted_templates["type"].append(key)
                            formatted_templates["templates"].append(file)
                else:
                    logger.warning(f"Path does not exist: {val}")
            if formatted_templates["type"]:
                logger.info(
                    "\nHyperSec DFE - List of Vector Templates\n"
                    + tabulate(formatted_templates, headers="keys", tablefmt="grid")
                )
            else:
                logger.info("No vector templates found.")
        return formatted_templates

    @staticmethod
    def build_ingestion_pipelines(
        args_dfe_package_file_path: str,
        args_ingestion_output_path: Optional[str],
        args_log_path: str,
        args_core_config: str = None,
        args_pipeline_template: Optional[str] = None,
        args_extra_config: Optional[dict] = {},
        args_build_core: bool = False,
        args_download: bool = False,
    ) -> None:
        """
        Render Jinja2 templates using the given configuration file.

        Parameters:
            args_dfe_package_file_path (str): The path to the dfe_package file.
            args_log_path (Optional[str]): Path to ingestion output.
            args_tmp/logs/ (str): Path to common directory for logs.

        Returns:
            None

        Raises:
            FileNotFoundError: If the dfe_package.yaml file is not found.
            Exception: If there is an error loading or processing the dfe_package file.
            ValueError: If no ingestion pipelines are found in the dfe_package.
        """
        logger = logger
        try:
            print("Attempting to load default DFE package configuration...")
            if not args_core_config:
                args_core_config = os.path.join(
                    os.path.dirname(__file__), 'core_config.yaml'
                )
            
            default_dfe_config = DFEConfigLoader.load_dfe_package(
                config_file_path=args_core_config
            )
        except FileNotFoundError:
            logger.error(
                f"Default DFE package file not found at {args_core_config}. "
                "Proceeding with an empty configuration."
            )
            # If the default DFE package file is not found, use an empty configuration
            if args_core_config:
                default_dfe_config = {}

        if not args_build_core:    
            default_dfe_config.pop("ingestion_pipelines")
            print("Attempting to load DFE package configuration...")
            try:
                dfe_config = DFEConfigLoader.load_dfe_package(
                    config_file_path=args_dfe_package_file_path
                )
                # only keep the ingestion pipelines from the incoming config
                default_dfe_config.pop("ingestion_pipelines", None)
            except FileNotFoundError as error:
                logger.error(
                    f"Could not find the {args_dfe_package_file_path} file, see error: {error}",
                    exc_info=True,
                )
        else:
            dfe_config = {}
        dfe_config = merge_configs(
            default_yaml=default_dfe_config, override_yaml=dfe_config
        )
        ingestion_output_path = (
            args_ingestion_output_path
            or dfe_config["global_settings"]["output"] or "./pipelines/core"
        )
        os.makedirs(ingestion_output_path, exist_ok=True)
        
        # if download is set, download the templates
        if args_download:
            logger.info(
                "Downloading vector templates as per the --download flag or the templates could not be found locally"
            )
            # Download the vector templates if the flag is set
            PipelineBuilderController.download_templates(
                args_log_path=args_log_path,
                args_output=dfe_config["global_settings"]["vector_files"].get("core").split("/src/")[0],
            )
            logger.info("Vector templates downloaded successfully.")
        pipeline_builder = PipelineBuilder(
            dfe_config=dfe_config,
            ingestion_output_path=ingestion_output_path,
            ingestion_pipeline_template_path=args_pipeline_template,
            logger=logger,
            extra_config=args_extra_config,
        )
        pipeline_builder.build()
        logger.info(
            "All Ingestion Pipeline Templates have been successfully rendered."
        )


    @staticmethod
    def download_templates(
        args_log_path: Optional[str] = "./tmp/logs",
        args_output: Optional[str] = "./hs_artefacts",
        args_repo_url: Optional[str] = None,
        args_version: Optional[str] = None,
        args_username: Optional[str] = None,
        args_password: Optional[str] = None
    ) -> None:
        logger = logger
        output_path = os.path.join(args_output)
        os.makedirs(output_path, exist_ok=True)
        # Download zip file from the repository
        repo_url = args_repo_url or os.getenv("ARTIFACTORY_VECTOR_TEMPLATES")
        username = args_username or os.getenv("ARTIFACTORY_USERNAME")
        password = args_password or os.getenv("ARTIFACTORY_PASSWORD")
        args_version = args_version or os.getenv("TEMPLATES_VERSION", "latest")
        full_repo_url = f"{repo_url}/artefacts-{args_version}.zip"
        output_filename = os.path.join(output_path, f"artefacts-{args_version}.zip")
        if not all ([repo_url, username, password]):
            logger.error(
                "Repository URL, username, and password must be provided to download templates. Either pass them as arguments or set them as environment variables. ARTIFACTORY_VECTOR_TEMPLATES, ARTIFACTORY_USERNAME, and ARTIFACTORY_PASSWORD."
            )
            raise ValueError(
                "Repository URL, username, and password must be provided to download templates. Either pass them as arguments or set them as environment variables. ARTIFACTORY_VECTOR_TEMPLATES, ARTIFACTORY_USERNAME, and ARTIFACTORY_PASSWORD."
            )
        try:
            logger.info(
                f"Downloading vector templates from {full_repo_url} to {output_filename}..."
            )
            with httpx.stream("GET", full_repo_url, auth=(username, password), follow_redirects=True, timeout=120.0) as response:
                # Raise an exception if the request failed (e.g., 404 Not Found, 401 Unauthorized)
                response.raise_for_status()
        
                # Open the output file in binary write mode ('wb')
                with open(output_filename, "wb") as f:
                    # Iterate over the response in chunks and write to the file
                    for chunk in response.iter_bytes():
                        f.write(chunk)
    
        except httpx.HTTPStatusError as e:
            logger.error(f"Error: {e}", exc_info=True)
            raise RuntimeError(
                f"Failed to download the file from {full_repo_url}. "
                f"HTTP Status: {e.response.status_code}, Reason: {e.response.reason_phrase}"
            ) from e
        except httpx.RequestError as e:
            logger.error(f"Error: {e}", exc_info=True)
            raise RuntimeError(
                f"Failed to download the file from {full_repo_url}. "
                f"Request error: {str(e)}"
            ) from e
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            raise RuntimeError(
                f"An unexpected error occurred while downloading the file from {full_repo_url}. "
                f"Error: {str(e)}"
            ) from e
        logger.info(
            f"Downloaded vector templates from {full_repo_url} to {output_filename}. Unzipping..."
        )
        try: 
            with ZipFile(output_filename, "r") as zip_ref:
                zip_ref.extractall(output_path)
            # remove the downloaded zip file after extraction
            os.remove(output_filename)
            logger.info(f"Unzipped files to {output_path}.")
        except Exception as e:
            logger.error(f"Error unzipping files: {e}", exc_info=True)
            raise RuntimeError(
                f"Failed to unzip the downloaded file {output_filename}. Error: {str(e)}"
            ) from e
        logger.info("Vector templates downloaded and extracted successfully.")