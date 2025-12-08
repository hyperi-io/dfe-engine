import os
from jinja2 import Environment, TemplateSyntaxError
from ...yaml_utils import yaml_dump_string, yaml_load_string, YAMLError
from hs_lib.logger import logger


class HuntValidator:
    """
    A class that provides static methods for validating hunt configurations and related syntax.
    """

    def validate_hunt_configuration(
        hunt_data: dict,
        env: Environment,
        rule_repo_dir: str,
        checkpoint_timestamp_field: str,
    ):
        """
        Validates the hunt configuration for correctness.

        :param hunt_data: A dictionary containing hunt configuration.
        :param env: A Jinja2 Environment instance for SQL template rendering.
        :param rule_repo_dir: The directory where rule templates are stored.
        :param logger: A logging instance to log messages.
        """
        logger.info("--- HUNT VALIDATOR STARTED ---")
        logger.info(f"Hunt Config:\n {hunt_data} ")
        try:
            yaml_load_string(yaml_dump_string(hunt_data))
        except YAMLError as e:
            raise ValueError(f"Invalid YAML structure: {e}") from e

        # cron_expressions = hunt_data.get("cron", [])
        # if not isinstance(cron_expressions, list):
        #     raise ValueError(f"Cron expressions should be a list, but got {type(cron_expressions).__name__}")

        # for cron_expression in cron_expressions:
        #     if not croniter.croniter.is_valid(cron_expression):
        #         raise ValueError(f"Invalid cron expression: {cron_expression}")

        if "log_buffer" not in hunt_data:
            raise ValueError("Missing 'log_buffer' in hunt configuration.")

        log_buffer = hunt_data.get("log_buffer")
        if not isinstance(log_buffer, int) or log_buffer <= 0:
            raise ValueError(
                f"Invalid 'log_buffer' value: {log_buffer}. It should be a positive integer."
            )

        global_target_table_name = hunt_data.get("global_target_table_name")
        if not isinstance(global_target_table_name, str) or not global_target_table_name.strip():
            raise ValueError("Invalid 'global_target_table_name'. It should be a non-empty string.")

        global_source_table_name = hunt_data.get("global_source_table_name")
        if not isinstance(global_source_table_name, str) or not global_source_table_name.strip():
            raise ValueError("Invalid 'global_source_table_name'. It should be a non-empty string.")

        if (
            not isinstance(checkpoint_timestamp_field, str)
            or not checkpoint_timestamp_field.strip()
        ):
            raise ValueError(
                "Invalid 'checkpoint_timestamp_field'. It should be a non-empty string."
            )

        customers = hunt_data.get("customers")
        if not customers or not isinstance(customers, list):
            raise ValueError("Customers list is missing or invalid.")

        rules = hunt_data.get("rules", [])
        for rule_info in rules:
            rule_name = rule_info.get("rule_name")
            rule_path = os.path.join(rule_repo_dir, f"{rule_name}.jinja2")
            if not os.path.isfile(rule_path):
                raise ValueError(f"Rule file does not exist: {rule_path}")
            HuntValidator.validate_rule_syntax(rule_path, env)

            initial_checkpoint_lookback_minutes = rule_info.get(
                "initial_checkpoint_lookback_minutes"
            )
            if not isinstance(initial_checkpoint_lookback_minutes, int):
                logger.warning(
                    "Missing 'initial_checkpoint_lookback_minutes' value in Hunt Config. Default Value will be used."
                )
            if (
                initial_checkpoint_lookback_minutes is not None
                and initial_checkpoint_lookback_minutes <= 0
            ):
                raise ValueError(
                    f"Invalid 'initial_checkpoint_lookback_minutes' value: {initial_checkpoint_lookback_minutes}. It should be a positive integer."
                )

        customer_filters = hunt_data.get("customer_filters", {})
        if not isinstance(customer_filters, dict):
            raise ValueError("Customer filters should be a dictionary.")

        for customer in customers:
            if customer not in customer_filters:
                logger.warning(f"Customer filters missing for customer: {customer}")
                continue

            if not isinstance(customer_filters[customer], dict):
                raise ValueError(f"Filters for customer '{customer}' should be a dictionary.")

            rules_filters = customer_filters[customer].get("rules", [])
            for rule_filter in rules_filters:
                if "name" not in rule_filter or "filter_clause" not in rule_filter:
                    raise ValueError(
                        f"Invalid rule filter structure for customer '{customer}': {rule_filter}"
                    )

        logger.info("--- HUNT VALIDATOR COMPLETED ---")

    @staticmethod
    def validate_rule_syntax(rule_path: str, env: Environment):
        """
        Validates the syntax of a Jinja2 SQL template.

        :param rule_path: The path to the Jinja2 SQL template file.
        :param env: A Jinja2 Environment instance for template rendering.
        """
        try:
            with open(rule_path, "r") as file:
                template_content = file.read()
                env.parse(template_content)
        except TemplateSyntaxError as e:
            raise ValueError(f"Syntax error in rule file {rule_path}: {e}") from e
