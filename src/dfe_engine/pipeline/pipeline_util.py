import os
import re
from enum import Enum


class VectorStepType(Enum):
    """The type of vector step"""

    BASE = "hs"
    HYPERI = "hyperi"
    SOURCE = "0"
    TRANSFORM = "1"
    SINK = "2"


class PipelineSchemaError(Exception):
    """Custom exception class for PipelineSchema errors."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


def find_vector_type(vector_step: str) -> VectorStepType:
    """
    Finds and returns the matching VectorStepType for a given vector step string.

    Args:
        vector_step (str): The vector step string to be matched.

    Returns:
        VectorStepType: The matching VectorStepType if found, otherwise None.
    """
    # return the matching VectorStepType if the str begins with the value
    for vector_type in VectorStepType:
        if vector_step.startswith(vector_type.value):
            return vector_type
    return None


def get_vector_step_type(vector_step: str, package_config: dict) -> str:
    """
    Retrieve the version and type of a given vector step from the package configuration.

    Args:
        vector_step (str): The name of the vector step to look up.
        package_config (dict): The package configuration dictionary containing vector templates and settings.

    Returns:
        template_type (str): The type of the vector step template, if found.

    Raises:
        PipelineSchemaError: If the specified vector step is not found in the package configuration.
    """
    # get the base folder for vectors
    template_type = None
    found_template = False
    paths = []
    for key, value in package_config.get("global_settings", {}).get("vector_files", {}).items():
        paths.append(value)
        if os.path.exists(value) and vector_step in os.listdir(value):
            found_template = True
            template_type = key
            return template_type
    if not found_template:
        raise PipelineSchemaError(
            f"Vector {vector_step} not found. Make sure your template is located in either {paths}"
        )


def read_vector_step_config(vector_step: str, package_config: dict) -> str:
    """
    Reads the configuration for a given vector step from the specified package configuration.

    Vector config files contain shell-style variable substitution (${VAR}) which is not
    valid YAML. This function returns the raw text content for regex-based extraction
    of environment variables.

    Args:
        vector_step (str): The name of the vector step to read the configuration for.
        package_config (dict): The package configuration dictionary containing global settings and paths.

    Returns:
        str: The raw text content of the vector step configuration.

    Raises:
        PipelineSchemaError: If the base path for the vector type is not found in the package configuration.
        PipelineSchemaError: If the vector configuration file does not exist at the constructed path.
    """
    # get the base folder for vectors
    template_type = get_vector_step_type(vector_step, package_config)
    base_path = package_config.get("global_settings", {}).get("vector_files", {}).get(template_type)
    full_path = os.path.join(base_path, vector_step)

    # read the config file for the vector step
    if not os.path.exists(full_path):
        raise PipelineSchemaError(
            f"Vector config file {full_path} not found. Make sure the template exists or you are using the correct version"
        )
    # Read as raw text - Vector configs contain ${VAR} syntax that isn't valid YAML
    with open(full_path) as f:
        return f.read()


def get_required_env_vars(vector_step: str, package_config: dict) -> dict:
    """
    Extracts required environment variables from a vector step configuration.

    This function reads the configuration for a given vector step and extracts
    environment variables that are required. The environment variables are
    identified by the pattern `${VAR_NAME}` or `${VAR_NAME:-default_value}` in
    the configuration.

    Args:
        vector_step (str): The name of the vector step to read the configuration for.
        package_config (dict): The package configuration dictionary.

    Returns:
        dict: A dictionary where the keys are the names of the required environment
              variables and the values are either None (if no default value is provided)
              or the default value specified in the configuration.
    """
    vector_config = read_vector_step_config(vector_step, package_config)
    env_vars = re.findall(r"\${(.*?)}", str(vector_config))
    required_environment_variables = {}
    for env_var in env_vars:
        # env var can't start with a number
        if not env_var[0].isdigit():
            env_var_name = env_var.split(":")[0]
            if (
                env_var.endswith(":?err")
                or env_var.endswith("?:err")
                or len(env_var.split(":")) == 1
            ):
                if "?" in env_var_name:
                    env_var_name = env_var_name.replace("?", "")
                required_environment_variables[env_var_name] = None

            else:
                required_environment_variables[env_var_name] = env_var.split(":-")[1]
    return required_environment_variables


def get_vector_pipelines(package_config: dict) -> list:
    """
    Retrieve the vector pipelines from the given package configuration.

    Args:
        package_config (dict): The package configuration dictionary containing pipeline information.

    Returns:
        list: A list of vector pipelines specified in the package configuration. If no pipelines are found, an empty list is returned.
    """
    vector_pipelines = package_config.get("ingestion_pipelines", [])
    return vector_pipelines


def get_pipeline(package_config: dict, pipeline_name: str) -> list:
    """
    Retrieve a specific pipeline from the package configuration.

    Args:
        package_config (dict): The configuration dictionary containing pipeline definitions.
        pipeline_name (str): The name of the pipeline to retrieve.

    Returns:
        list: The pipeline configuration corresponding to the specified pipeline name.

    Raises:
        PipelineSchemaError: If the specified pipeline name is not found in the package configuration.
    """
    vector_pipelines = get_vector_pipelines(package_config)
    if pipeline_name in vector_pipelines:
        return vector_pipelines[pipeline_name]
    else:
        raise PipelineSchemaError(
            f"Pipeline {pipeline_name} not found in package config. Make sure the pipeline exists and is defined in the configuration file"
        )


def vector_step_name_to_input(vector_step: str) -> str:
    """
    Converts a vector step name to an input name format.

    Args:
        vector_step (str): The vector step name, expected to be in the format "step-name.yaml" or simply "step-name".

    Returns:
        str: The formatted input name, with the step name converted to uppercase, hyphens replaced with underscores,
             and prefixed with an underscore and suffixed with "_INPUT". This is the standard input format for all Vector Transforms unless otherwise stated.
    """
    return f"_{vector_step.split('.')[0].replace('-', '_').upper()}_INPUT"


def vector_step_name_to_output(vector_step: str) -> str:
    """
    Converts a vector step name to an output name.

    This function takes a vector step name in the format of a string and converts it into the output of the vector.

    Args:
        vector_step (str): The vector step name to be converted.

    Returns:
        str: The converted output name.
    """
    return vector_step.split(".")[0].replace("-", "_").lower()


def gather_env_variables_for_pipeline(package_config: dict, pipeline_name: str) -> tuple:
    """
    Gathers and merges environment variables required for a specific pipeline.

    This function retrieves global environment variables from the package configuration
    and overrides them with environment variables specific to the given pipeline. It also
    collects required environment variables for each step in the pipeline, ensuring that
    inputs are properly defined or defaulted to the output of the previous step if not specified.

    Args:
        package_config (dict): The configuration dictionary containing global and pipeline-specific settings.
        pipeline_name (str): The name of the pipeline for which to gather environment variables.

    Returns:
        Tuple[dict, dict]: A tuple containing two dictionaries:
            - The first dictionary contains all required environment variables for the pipeline.
            - The second dictionary contains only the environment variables that are specific to vector steps.
    """
    # Get a copy of global env vars to avoid modifying the original
    global_env_vars = dict(package_config.get("default_env_vars", {}))
    pipeline_config = get_pipeline(package_config, pipeline_name)
    steps = pipeline_config.get("steps", [])
    pipeline_env_vars = pipeline_config.get("env", {})
    pipeline_meta = pipeline_config.get("meta", {})
    # combine env_var and meta vars
    pipeline_env_vars.update(pipeline_meta)

    # Initialize required env vars with global vars
    required_env_vars = dict(global_env_vars)
    # Update with pipeline-specific vars (they take precedence)
    required_env_vars.update(pipeline_env_vars)

    # Gather step-specific env vars
    step_env_vars = {}
    for i, step in enumerate(steps):
        vector_step = step["id"]
        # Get env vars required by this step
        step_vars = get_required_env_vars(vector_step, package_config)
        step_env_vars.update(step_vars)

        # Handle step input
        if "input" in step:
            step_env_vars[vector_step_name_to_input(vector_step)] = step["input"]
        # Use previous step's output as input if not specified
        elif (
            find_vector_type(vector_step)
            in (VectorStepType.TRANSFORM, VectorStepType.SINK, VectorStepType.BASE)
            and i > 0
        ):
            input_name = vector_step_name_to_input(vector_step)
            if input_name in step_vars and input_name not in required_env_vars:
                step_env_vars[input_name] = [vector_step_name_to_output(steps[i - 1]["id"])]

    # Update required_env_vars with step-specific vars
    required_env_vars.update(step_env_vars)

    # Fill in any None values with global defaults
    for key, value in required_env_vars.items():
        if value is None and key in global_env_vars:
            required_env_vars[key] = global_env_vars[key]
    # vector vars
    vector_env_vars = {
        key: value for key, value in required_env_vars.items() if key in step_env_vars
    }
    return required_env_vars, vector_env_vars


def merge_configs(default_yaml: dict, override_yaml: dict) -> dict:
    """
    Recursively merges two dictionaries.

    - Scalar values in the override yaml dictionary overwrite scalar values in the default yaml.
    - Lists are extended and deduplicated.
    - Dictionaries are recursively merged.

    :param default_yaml: The base dictionary to merge into.
    :param override_yaml: The dictionary to merge on top of the base.
    :return: A new dictionary that is the result of merging the two.

    """
    # Start with a copy of the base dictionary
    merged = default_yaml.copy()

    for key, overlay_value in override_yaml.items():
        if key in merged:
            base_value = merged[key]
            # If both values are dictionaries, recurse
            if isinstance(base_value, dict) and isinstance(overlay_value, dict):
                merged[key] = merge_configs(base_value, overlay_value)
            # If both values are lists, extend and deduplicate
            elif isinstance(base_value, list) and isinstance(overlay_value, list):
                # Extend the base list with items from the overlay list
                extended_list = base_value + overlay_value
                # Deduplicate while preserving order
                deduped_list = []
                for item in extended_list:
                    if item not in deduped_list:
                        deduped_list.append(item)
                merged[key] = deduped_list
            # Otherwise, the overlay value replaces the base value
            else:
                merged[key] = overlay_value
        else:
            # If the key is not in the base, just add it
            merged[key] = overlay_value

    return merged
