import os
import jinja2
import re
from jinja2 import Environment, FileSystemLoader, meta
from .pipeline_util import (
    gather_env_variables_for_pipeline,
    PipelineSchemaError,
)
from typing import Dict
from hs_lib.logger import logger
from ..yaml_utils import yaml_load_string, YAMLError


class Pipeline:
     # These keys are set by secrets during run time so we don't need to check them here
    SECRETS_VARS = [
        "CH_HOST",
        "CH_PASSWORD",
        "CH_USERNAME",
        "CLICKHOUSE_AUTH_USER",
        "CLICKHOUSE_AUTH_PASSWORD",
        "CLICKHOUSE_ENDPOINT",
        "KAFKA_BROKERS",
        "KAFKA_BROKERS_SASL",
        "KAFKA_BROKERS_SASL_IAM",
        "KAFKA_BROKERS_SASL_SCRAM",
        "KAFKA_SASL_USERNAME",
        "KAFKA_SASL_PASSWORD",
        "VECTOR_RECEIVER_TOKEN",
        "VECTOR_RECEIVER_AUTH"
    ]
    def __init__(
        self,
        name: str,
        dfe_config: Dict,
        pipeline_config: Dict,
        output_dir: str,
        logger=None,
        ingestion_pipeline_template_path: str = None,
        extra_config: Dict = {},
    ):
        """
        Initialize the Pipeline instance.

        Args:
            name (str): The name of the pipeline.
            output_dir (str): The directory where the pipeline configurations will be written.
            dfe_config (Dict): A dictionary containing the DFE configuration settings.
            pipeline_config (Dict): A dictionary containing the pipeline configuration settings.
            logger (Logger, optional): The logger object for logging messages. Defaults to None.
        """
        self.name = name.lower()
        self.output_dir = output_dir
                self.dfe_config = dfe_config
        self.global_settings = dfe_config.get("global_settings", {})
        self.default_env_vars = dfe_config.get(
            "default_env_vars", {}
        )
        self.pipeline_config = pipeline_config
        template_path = dfe_config.get("global_settings", {}).get(
            "helm_template"
        ) if not ingestion_pipeline_template_path else ingestion_pipeline_template_path
        
        # Make template path absolute if it's relative
        if template_path and not os.path.isabs(template_path):
            # Resolve relative to the dfe_pipelinebuilder directory
            template_path = os.path.abspath(template_path)
        
        self.pipeline_template = template_path
        self.extra_config = extra_config


    def combine_global_and_pipeline_env_vars(self) -> Dict:
        """
        Combines the global environment variables with the pipeline environment variables.
        Global args are used as base, and pipeline-specific env vars override them.

        Returns:
            Dict: A dictionary containing the combined environment variables.
        """
        # Create a new dict to avoid modifying the global args
        pipeline_env_vars = self.pipeline_config.get("env", {})
        # Start with a copy of global args and update with pipeline-specific vars
        combined_vars = dict(self.default_env_vars)
        combined_vars.update(pipeline_env_vars)
        # If extra_config is provided, update combined_vars with it
        if self.extra_config:
            combined_vars.update(self.extra_config)
        return combined_vars

    def get_pipeline_template(self):
        """
        Retrieves and returns a Jinja2 template for the pipeline.

        This method loads a Jinja2 template from the file path specified in the
        global settings under the key 'helm_template'. It sets up
        the Jinja2 environment with file system loaders pointing to the directory
        containing the template and the directories containing the step templates,
        and uses strict undefined variable handling.

        Returns:
            jinja2.Template: The loaded Jinja2 template object.
        """
        if not self.pipeline_template:
            logger.error("No pipeline template provided using the default template")
            self.pipeline_template = os.path.join(
                    os.path.dirname(__file__), 'pipeline_template.yaml'
                )
        logger.debug(f"Loading Pipeline Template from {self.pipeline_template}")
        # Get main template directory
        template_dir = os.path.dirname(self.pipeline_template)

        # Create a FileSystemLoader with all search paths
        logger.info(f"Loading template from: {self.pipeline_template}")
        # autoescape is disabled for YAML templates - HTML escaping produces invalid YAML
        # (e.g., & becomes &#38; which breaks YAML anchor/alias syntax)
        # B701 is about XSS in HTML templates, not applicable for YAML generation
        env = Environment(
            loader=FileSystemLoader(template_dir),
            undefined=jinja2.StrictUndefined,
            autoescape=False,
        )
        template = env.get_template(os.path.basename(self.pipeline_template))
        return template

    def render_template_for_pipeline(self, env_vars: dict, vector_env_vars: dict) -> str:
        """
        Renders the template for the specified pipeline.

        Args:
            pipeline_name (str): The name of the pipeline.
            env_vars (dict): A dictionary containing the environment variables.
            vector_env_vars (dict): A dictionary containing the environment variables specific to vector steps.

        Returns:
            str: The rendered template.
        """
        template = self.get_pipeline_template()
        step_full_paths = []
        for step in self.pipeline_config["steps"]:
            # Form the full path using the step name, version, and file name
            fully_qualified_step = step["id"] if step['id'].endswith('.yml') or step['id'].endswith('.yaml') else f"{step['id']}.yml"

            # Apply additional path modifications if needed
            template_path = self.global_settings.get("ip_templates_path", "core_templates")
            mount_path = self.global_settings.get("vector_config_mount_path", "/etc/vector")
            fully_qualified_step = os.path.join(
                    mount_path,
                    template_path,
                    fully_qualified_step
                )

            step_full_paths.append(fully_qualified_step)

        # for geoip files we need to provide the full path to the file
        geoip_enrichment_path_key = "VECTOR_GEOIP_PATH"
        if geoip_enrichment_path_key in env_vars:
            vector_mount_path = self.global_settings.get("vector_config_mount_path", "/etc/vector")
            geoip_path = self.global_settings.get("ip_config_geo_ip_path", "geoip_mappings")
            if vector_mount_path not in env_vars[geoip_enrichment_path_key]:
                env_vars[geoip_enrichment_path_key] = os.path.join(
                    vector_mount_path,
                    geoip_path
                )

        # for vector enrichment files add the mount path if not already present
        standard_enrichment_path_key = "VECTOR_ENRICHMENT_PATH"
        vector_mount_path = self.global_settings.get("vector_config_mount_path", "/etc/vector")
        enrichment_path = self.global_settings.get("ip_config_standard_enrichment_path", "standard_mappings")
        if standard_enrichment_path_key in env_vars and vector_mount_path not in env_vars[standard_enrichment_path_key]:
            env_vars[standard_enrichment_path_key] = os.path.join(
                vector_mount_path,
                enrichment_path
            )        

        str_env_vars = self.convert_all_env_vars_to_str(env_vars)

        # Remove secrets from env_vars if they are in the SECRETS_VARS list
        for key in list(str_env_vars.keys()):
            if key in self.SECRETS_VARS:
                str_env_vars.pop(key, None)
 
        # Gather all variables from the template
        with open(self.pipeline_template, 'r') as f:
            template_source = f.read()
        parsed_template = template.environment.parse(template_source)
        all_variables = meta.find_undeclared_variables(parsed_template)
        
        # Find variables with default values using regex
        # Pattern to match variables with defaults anywhere in expressions
        # Matches: VAR | default("value") or VAR | default(value, true)
        default_patterns = [
            r'([a-zA-Z_][a-zA-Z0-9_]*)\s*\|\s*default\s*\(\s*[\'"]?([^\'",\)]*)[\'"]?\s*\)',
            r'([a-zA-Z_][a-zA-Z0-9_]*)\s*\|\s*default\s*\(\s*[\'"]?([^\'",\)]*)[\'"]?\s*,\s*[^)]*\)',
            r'([a-zA-Z_][a-zA-Z0-9_]*)\s*\|\s*default\s*:\s*[\'"]?([^\'"\}]*)[\'"]?'
        ]
        
        variables_with_defaults = {}
        for pattern in default_patterns:
            for match in re.finditer(pattern, template_source):
                var_name = match.group(1)
                default_value = match.group(2)
                variables_with_defaults[var_name] = default_value
        
        # Find variables that are used conditionally (optional variables)
        conditional_pattern = r'\{\%\s*if\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\%\}'
        optional_variables = set()
        for match in re.finditer(conditional_pattern, template_source):
            var_name = match.group(1)
            optional_variables.add(var_name)
        
        # Also check for variables used with 'is defined' checks
        defined_check_pattern = r'\{\%\s*if\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+is\s+defined\s*\%\}'
        for match in re.finditer(defined_check_pattern, template_source):
            var_name = match.group(1)
            optional_variables.add(var_name)
        
        for var in all_variables:
            if var not in env_vars and var not in ('steps', 'NAME', 'env_vars', 'config_maps'):
                if var in self.global_settings:
                    env_vars[var] = self.global_settings[var]
                elif var in os.environ:
                    env_vars[var] = os.environ[var]
                elif var in variables_with_defaults:
                    logger.debug(f"Using default value for {var}: {variables_with_defaults[var]}")
                    # Don't set the variable - let Jinja handle the default
                elif var in optional_variables:
                    logger.debug(f"Skipping optional variable {var} - not defined")
                    # Don't set the variable - it's optional and will be handled by template conditionals
                else:
                    raise PipelineSchemaError(
                        f"Variable {var} is not defined in the pipeline or global settings or in the .env file."
                    )
        rendered_template = template.render(
            NAME=self.name.lower().replace("_", "-"),
            env_vars={k: v for k, v in str_env_vars.items() if k in vector_env_vars or k in self.pipeline_config.get("env", {})},
            config_maps={k: v for k, v in str_env_vars.items() if k in vector_env_vars or k in self.pipeline_config.get("env", {})},
            steps=step_full_paths,
            **env_vars,
        )
        return rendered_template

    def gather_env_variables_for_pipeline(self) -> Dict:
        """
        Gathers the environment variables for the specified pipeline.

        Returns:
            Dict: A dictionary containing the environment variables.
        """
        # Get base environment variables for this pipeline
        env_vars, self.vector_env_vars = gather_env_variables_for_pipeline(self.dfe_config, self.name)
        env_vars = {key.upper(): value for key, value in env_vars.items()}

        # Get pipeline-specific environment variables
        pipeline_env_vars = self.combine_global_and_pipeline_env_vars()

        # Update env_vars with pipeline-specific variables
        for key, value in pipeline_env_vars.items():
            env_vars[key.upper()] = value
        # Set the kafka consumer group to the pipeline name if not set
        if (
            "KAFKA_CONSUMER_GROUP" not in env_vars
            or env_vars["KAFKA_CONSUMER_GROUP"] is None
        ):
            env_vars["KAFKA_CONSUMER_GROUP"] = f"{self.name.lower().replace('_', '-')}"
        # Check if any key is None, raise an error if any
        for key, value in env_vars.items():
            if value is None:
                # if key is available as environment variable, use that as last resort
                if key in self.global_settings:
                    env_vars[key] = self.global_settings[key]
                elif key in os.environ:
                    env_vars[key] = os.environ[key]
                elif key in self.SECRETS_VARS:
                    pass  # These are set by secrets during run time so we don't need to check them here
                else:
                    raise PipelineSchemaError(
                        f"Env var {key} is None. You need to define this env var in the pipeline or global settings or in the .env file."
                    )
        return env_vars, self.vector_env_vars

    def convert_all_env_vars_to_str(self, env_vars: Dict) -> Dict:
        """
        Converts all the environment variables to strings.

        Args:
            env_vars (Dict): A dictionary containing the environment variables.

        Returns:
            Dict: A dictionary containing the environment variables with all values as strings.
        """
        # if the var looks like a list then wrap it in a quotes
        str_env_vars = {}
        for k, v in env_vars.items():
            if isinstance(v, str):
                if '"' in v:
                    str_env_vars[k] = f"'{v}'"
                else:
                    str_env_vars[k] = f'"{v}"'
            elif not isinstance(v, str):
                str_env_vars[k] = f'"{str(v)}"'
            else:
                str_env_vars[k] = v
        return str_env_vars

    def write_pipeline_config(self, rendered_template: str):
        """
        Writes the rendered pipeline configuration to a file.

        Args:
            rendered_template (str): The rendered pipeline configuration.
        """
        output_file = os.path.join(
            self.output_dir, f"{self.name.replace('_', '-').lower()}.yaml"
        )
        output_folder = os.path.dirname(output_file)
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)
        # Write the rendered template to the output file and also validate if the written file is a valid YAML file
        with open(output_file, "w") as f:
            f.write(rendered_template)
            logger.info(f"Pipeline configuration written to {output_file}")

        # Validate if the written file is a valid YAML file
        # Vector configs use ${VAR} syntax which YAML parsers interpret as invalid anchors
        # We temporarily escape these before validation
        with open(output_file, "r") as f:
            content = f.read()
            # Escape ${...} patterns so YAML parser doesn't treat them as anchors
            escaped_content = re.sub(r'\$\{', r'__DOLLAR_BRACE__', content)
            try:
                yaml_load_string(escaped_content)
            except YAMLError as e:
                error_msg = f"Error validating written pipeline configuration: {e}"
                logger.error(error_msg)
                raise PipelineSchemaError(error_msg)
        logger.info(f"Pipeline configuration written to {output_file}")

    def build(self):
        """
        Builds the pipeline configuration by gathering environment variables,
        rendering the pipeline template with these variables, and writing the
        rendered template to the pipeline configuration file.

        Steps:
        1. Gather environment variables for the pipeline.
        2. Render the pipeline template using the gathered environment variables.
        3. Write the rendered template to the pipeline configuration file.
        Returns:
            None
        """
        env_vars, vector_env_vars = self.gather_env_variables_for_pipeline()
        rendered_template = self.render_template_for_pipeline(env_vars, vector_env_vars)
        self.write_pipeline_config(rendered_template)
