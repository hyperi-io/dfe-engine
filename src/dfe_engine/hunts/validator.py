import os
from typing import Any

from jinja2 import Environment, TemplateSyntaxError
from scalo.logger import logger

from ..yaml_utils import YAMLError, yaml_dump_string, yaml_load_string


class HuntValidator:
    """
    A class that provides static methods for validating hunt configurations and related syntax.
    """

    def validate_hunt_configuration(
        hunt_data: dict,
        env: Environment,
        rule_repo_dir: str,
        checkpoint_timestamp_field: str,
        source_registry: Any = None,
    ):
        """
        Validates the hunt configuration for correctness.

        :param hunt_data: A dictionary containing hunt configuration.
        :param env: A Jinja2 Environment instance for SQL template rendering.
        :param rule_repo_dir: The directory where rule templates are stored.
        :param checkpoint_timestamp_field: The timestamp field for checkpointing.
        :param source_registry: Optional SourceRegistry for validating source references.
        """
        logger.info("--- HUNT VALIDATOR STARTED ---")
        logger.info(f"Hunt Config:\n {hunt_data} ")

        HuntValidator._validate_yaml_structure(hunt_data)
        HuntValidator._validate_required_fields(hunt_data, checkpoint_timestamp_field)
        customers = HuntValidator._validate_customers(hunt_data)
        HuntValidator._validate_rules(hunt_data, env, rule_repo_dir, source_registry)
        HuntValidator._validate_customer_filters(hunt_data, customers)
        HuntValidator._validate_scheduling_fields(hunt_data)

        logger.info("--- HUNT VALIDATOR COMPLETED ---")

    @staticmethod
    def _validate_yaml_structure(hunt_data: dict) -> None:
        """Validate the YAML structure of hunt data."""
        try:
            yaml_load_string(yaml_dump_string(hunt_data))
        except YAMLError as e:
            raise ValueError(f"Invalid YAML structure: {e}") from e

    @staticmethod
    def _validate_required_fields(hunt_data: dict, checkpoint_timestamp_field: str) -> None:
        """Validate required fields in hunt configuration."""
        if "log_buffer" not in hunt_data:
            raise ValueError("Missing 'log_buffer' in hunt configuration.")

        log_buffer = hunt_data.get("log_buffer")
        if not isinstance(log_buffer, int) or log_buffer <= 0:
            raise ValueError(
                f"Invalid 'log_buffer' value: {log_buffer}. It should be a positive integer."
            )

        # Optional: the rule's query carries its own target. Present means non-empty.
        global_target_table_name = hunt_data.get("global_target_table_name")
        if global_target_table_name is not None and (
            not isinstance(global_target_table_name, str) or not global_target_table_name.strip()
        ):
            raise ValueError("Invalid 'global_target_table_name'. It should be a non-empty string.")

        # Source model: hunts can use 'source' (resolved via SourceRegistry) OR
        # 'global_source_table_name' (direct table reference). At least one required.
        global_source_table_name = hunt_data.get("global_source_table_name")
        has_source_ref = any(r.get("source") for r in hunt_data.get("rules", []))
        if not has_source_ref:
            if (
                not isinstance(global_source_table_name, str)
                or not global_source_table_name.strip()
            ):
                raise ValueError(
                    "Either 'global_source_table_name' or per-rule 'source' field is required."
                )

        if (
            not isinstance(checkpoint_timestamp_field, str)
            or not checkpoint_timestamp_field.strip()
        ):
            raise ValueError(
                "Invalid 'checkpoint_timestamp_field'. It should be a non-empty string."
            )

    @staticmethod
    def _validate_customers(hunt_data: dict) -> list:
        """Validate customers list and return it."""
        customers = hunt_data.get("customers")
        if not customers or not isinstance(customers, list):
            raise ValueError("Customers list is missing or invalid.")
        return customers

    @staticmethod
    def _validate_rules(
        hunt_data: dict, env: Environment, rule_repo_dir: str, source_registry: Any = None
    ) -> None:
        """Validate rules configuration and syntax."""
        rules = hunt_data.get("rules", [])
        for rule_info in rules:
            rule_name = rule_info.get("rule_name")
            rule_path = os.path.join(rule_repo_dir, f"{rule_name}.jinja2")
            if os.path.isfile(rule_path):
                HuntValidator.validate_rule_syntax(rule_path, env)
            else:
                # Rule file may be uploaded AFTER the hunt is drafted (create-then-add);
                # warn instead of blocking. Syntax is validated once the file exists.
                logger.warning(
                    f"Rule file not found for hunt rule '{rule_name}' at {rule_path}; "
                    "create-time existence check skipped (rule may be added later)."
                )

            # Validate source reference if SourceRegistry available
            source_name = rule_info.get("source")
            if source_name and source_registry:
                try:
                    source_registry.get_source(source_name)
                except Exception:
                    logger.warning(
                        f"Rule '{rule_name}' references source '{source_name}' "
                        f"which is not in the SourceRegistry."
                    )

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

    @staticmethod
    def _validate_customer_filters(hunt_data: dict, customers: list) -> None:
        """Validate customer filters configuration."""
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

    @staticmethod
    def _validate_scheduling_fields(hunt_data: dict) -> None:
        """Validate optional scheduling fields if present."""
        scheduling_mode = hunt_data.get("scheduling_mode")
        if scheduling_mode is not None and scheduling_mode not in ("adaptive", "cron"):
            raise ValueError(
                f"Invalid 'scheduling_mode': '{scheduling_mode}'. Must be 'adaptive' or 'cron'."
            )

        min_interval = hunt_data.get("min_interval_seconds")
        if min_interval is not None:
            if not isinstance(min_interval, (int, float)) or min_interval < 0:
                raise ValueError(f"Invalid 'min_interval_seconds': {min_interval}. Must be >= 0.")

    @staticmethod
    def validate_rule_syntax(rule_path: str, env: Environment):
        """
        Validates the syntax of a Jinja2 SQL template.

        :param rule_path: The path to the Jinja2 SQL template file.
        :param env: A Jinja2 Environment instance for template rendering.
        """
        try:
            with open(rule_path) as file:
                template_content = file.read()
                env.parse(template_content)
        except TemplateSyntaxError as e:
            raise ValueError(f"Syntax error in rule file {rule_path}: {e}") from e
