import os
import re
from importlib import resources
from pathlib import Path
from zipfile import ZipFile

from scalo.logger import logger
from tabulate import tabulate

from ..settings import get_settings
from ..storage import StorageError, get_storage_backend
from ..yaml_utils import yaml_load_string
from .pipeline_builder import PipelineBuilder
from .pipeline_util import merge_configs


def reject_escaping_members(names: list[str], output_path: str | Path) -> None:
    """Refuse an archive carrying a member that resolves outside *output_path*.

    ``zipfile`` silently rewrites such a member -- it drops the leading separator
    and every ``..`` component and writes the remainder into the destination -- so
    a tampered archive extracts quietly under a name nobody asked for. A rewritten
    path means the archive is not what was published, so it is refused here before
    anything is written.

    Raises:
        RuntimeError: when a member resolves outside the destination directory.
    """
    destination = Path(output_path).resolve()
    for name in names:
        resolved = (destination / name).resolve()
        if resolved != destination and destination not in resolved.parents:
            raise RuntimeError(
                f"Archive member {name!r} resolves outside the destination {destination}"
            )


def _load_yaml_config(config_file_path: str | None = None, require: bool = True) -> dict:
    """Load a YAML config file with environment variable substitution."""
    path = Path(config_file_path) if config_file_path else Path.cwd() / "dfe_package.yaml"
    if not path.exists():
        if require:
            raise FileNotFoundError(f"Configuration file [{path}] not found")
        return {}
    content = path.read_text()
    content = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), content)
    return yaml_load_string(content) or {}


class PipelineBuilderController:
    @staticmethod
    def get_resource_path(log, package: str, resource_path: str):
        try:
            return resources.files(package) / resource_path
        except FileNotFoundError:
            log.error(f"Resource does not exist: {package}/{resource_path}")
            return None

    @staticmethod
    def list_ingestion_templates(
        args_dfe_package_file_path: str,
        args_log_path: str,
        args_download: bool = False,
    ) -> dict[str, list[str]]:
        """
        List all vector templates from the provided DFE package file.

        Parameters:
            args_dfe_package_file_path (str): The path to the dfe_package file.
            args_log_path (str): Path to the log directory.

        Returns:
            Dict[str, List[str]]: A dictionary with template types as keys and lists of template names as values.
        """
        try:
            dfe_config_data = _load_yaml_config(args_dfe_package_file_path)
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

        vector_files = dfe_config_data.get("global_settings", {}).get("vector_files", {})
        if not vector_files:
            logger.warning("No vector files found in the configuration.")
            return {}
        if args_download:
            # Download the vector templates if the flag is set
            PipelineBuilderController.download_templates(
                args_log_path=args_log_path,
                args_output=dfe_config_data["global_settings"]["vector_files"]
                .get("core")
                .split("/src/")[0],
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
                    "\nHyperI DFE - List of Vector Templates\n"
                    + tabulate(formatted_templates, headers="keys", tablefmt="grid")
                )
            else:
                logger.info("No vector templates found.")
        return formatted_templates

    @staticmethod
    def build_ingestion_pipelines(
        args_dfe_package_file_path: str,
        args_ingestion_output_path: str | None,
        args_log_path: str,
        args_core_config: str = None,
        args_pipeline_template: str | None = None,
        args_extra_config: dict | None = None,
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
        if args_extra_config is None:
            args_extra_config = {}
        try:
            logger.debug("Attempting to load default DFE package configuration...")
            if not args_core_config:
                args_core_config = os.path.join(os.path.dirname(__file__), "core_config.yaml")

            default_dfe_config = _load_yaml_config(args_core_config)
        except FileNotFoundError:
            logger.error(
                f"Default DFE package file not found at {args_core_config}. "
                "Proceeding with an empty configuration."
            )
            # If the default DFE package file is not found, use an empty configuration
            if args_core_config:
                default_dfe_config = {}

        dfe_config: dict = {}
        if not args_build_core:
            default_dfe_config.pop("ingestion_pipelines", None)
            logger.debug("Attempting to load DFE package configuration...")
            try:
                dfe_config = _load_yaml_config(args_dfe_package_file_path)
                # only keep the ingestion pipelines from the incoming config
                default_dfe_config.pop("ingestion_pipelines", None)
            except FileNotFoundError as error:
                logger.error(
                    f"Could not find the {args_dfe_package_file_path} file, see error: {error}",
                    exc_info=True,
                )
                dfe_config = {}
        dfe_config = merge_configs(default_yaml=default_dfe_config, override_yaml=dfe_config)
        ingestion_output_path = (
            args_ingestion_output_path
            or dfe_config["global_settings"]["output"]
            or "./pipelines/core"
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
                args_output=dfe_config["global_settings"]["vector_files"]
                .get("core")
                .split("/src/")[0],
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
        logger.info("All Ingestion Pipeline Templates have been successfully rendered.")

    @staticmethod
    def download_templates(
        args_log_path: str | None = "./tmp/logs",
        args_output: str | None = "./hs_artefacts",
        args_repo_url: str | None = None,
        args_version: str | None = None,
        args_username: str | None = None,
        args_password: str | None = None,
    ) -> None:
        """
        Download templates from storage backend (local, HTTP, or S3).

        Auto-detects storage type from the URL/path:
        - Local paths (/path, ./path): Copy from local filesystem (for on-prem/Rancher with PVC mounts)
        - HTTP URLs: Download from Artifactory or HTTP server
        - S3 URIs (s3://bucket/path): Download from S3

        Args:
            args_log_path: Path to log directory
            args_output: Output directory for templates
            args_repo_url: Repository URL/path (auto-detected type)
            args_version: Template version
            args_username: Username for HTTP auth
            args_password: Password for HTTP auth
        """
        settings = get_settings()
        output_path = os.path.abspath(args_output)
        os.makedirs(output_path, exist_ok=True)

        # Determine source URL/path - use settings cascade
        repo_url = args_repo_url or settings.artifactory.url or settings.storage.path
        username = args_username or settings.artifactory.username
        password = args_password or settings.artifactory.password
        version = args_version or settings.artifactory.templates_version

        if not repo_url:
            raise ValueError(
                "No template source configured. Set DFE_ARTIFACTORY_URL (for HTTP), "
                "DFE_STORAGE_PATH (for local/S3), or pass args_repo_url."
            )

        # Get storage backend with auto-detection
        backend = get_storage_backend(repo_url, username=username, password=password)

        # Build remote path and local destination
        remote_file = f"artefacts-{version}.zip"
        output_filename = os.path.join(output_path, remote_file)

        try:
            logger.info(f"Fetching templates from {repo_url}/{remote_file}...")
            backend.download(remote_file, output_filename)
        except StorageError as e:
            logger.error(f"Failed to download templates: {e}", exc_info=True)
            raise RuntimeError(f"Failed to download templates: {e}") from e

        # Unzip the downloaded file
        logger.info(f"Extracting templates to {output_path}...")
        try:
            with ZipFile(output_filename, "r") as zip_ref:
                reject_escaping_members(zip_ref.namelist(), output_path)
                zip_ref.extractall(output_path)
            # Remove the zip file after extraction
            os.remove(output_filename)
            logger.info(f"Templates extracted to {output_path}")
        except Exception as e:
            logger.error(f"Error extracting templates: {e}", exc_info=True)
            raise RuntimeError(f"Failed to extract templates: {e}") from e

        logger.info("Templates downloaded and extracted successfully.")
