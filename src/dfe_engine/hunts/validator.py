from collections.abc import Callable
from pathlib import Path
from typing import Any

from scalo.logger import logger

from ..clickhouse.quoting import plain_source_name, plain_table_name
from ..yaml_utils import YAMLError, yaml_dump_string, yaml_load, yaml_load_string
from .rule_guard import refuse_direct_query
from .rule_names import rule_file


def rule_source(source_db: str, source_table: str) -> str:
    """A rule's ``source_db`` and ``source_table`` as one reference, or "" when it names no table.

    Args:
        source_db: The rule's database, empty when it names none.
        source_table: The rule's table.

    Returns:
        ``database.table``, the bare table when there is no database, or "".
    """
    if not source_table:
        return ""
    return f"{source_db}.{source_table}" if source_db else source_table


class HuntValidator:
    """
    A class that provides static methods for validating hunt configurations and related syntax.
    """

    def validate_hunt_configuration(
        hunt_data: dict,
        rules_dir: str | Path,
        checkpoint_timestamp_field: str,
        source_registry: Any = None,
    ):
        """
        Validates the hunt configuration for correctness.

        :param hunt_data: A dictionary containing hunt configuration.
        :param rules_dir: The directory holding each rule as ``{name}.yaml``, the
            file the hunt runner reads.
        :param checkpoint_timestamp_field: The timestamp field for checkpointing.
        :param source_registry: Optional SourceRegistry for validating source references.
        """
        logger.info("--- HUNT VALIDATOR STARTED ---")
        logger.info(f"Hunt Config:\n {hunt_data} ")

        HuntValidator._validate_yaml_structure(hunt_data)
        HuntValidator._validate_required_fields(hunt_data, checkpoint_timestamp_field)
        customers = HuntValidator._validate_customers(hunt_data)
        HuntValidator._validate_rules(hunt_data, rules_dir, source_registry)
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
        if global_target_table_name is not None:
            if (
                not isinstance(global_target_table_name, str)
                or not global_target_table_name.strip()
            ):
                raise ValueError(
                    "Invalid 'global_target_table_name'. It should be a non-empty string."
                )
            HuntValidator._validate_table_name(
                "'global_target_table_name'", global_target_table_name
            )

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
        # A blank one defers to the per-rule 'source'; anything else must name a table.
        if global_source_table_name is not None and str(global_source_table_name).strip():
            HuntValidator._validate_table_name(
                "'global_source_table_name'", global_source_table_name, split=plain_source_name
            )

        if (
            not isinstance(checkpoint_timestamp_field, str)
            or not checkpoint_timestamp_field.strip()
        ):
            raise ValueError(
                "Invalid 'checkpoint_timestamp_field'. It should be a non-empty string."
            )

        HuntValidator._validate_query(hunt_data.get("query"))

    @staticmethod
    def _validate_query(query: Any) -> None:
        """Refuse a direct ``query`` the hunt runner would drop.

        The hunts API never writes one, but a governance action can, and the
        runner sends it to ClickHouse as written.
        """
        if query is None:
            return
        if not isinstance(query, str):
            raise ValueError(f"Invalid 'query'. It should be SQL text, not {query!r}.")
        if not query.strip():
            return
        refusal = refuse_direct_query(query)
        if refusal is not None:
            raise ValueError(f"Invalid 'query': {refusal}")

    @staticmethod
    def _validate_table_name(
        label: str,
        value: Any,
        *,
        split: Callable[[str], tuple[str, str]] = plain_table_name,
    ) -> None:
        """Refuse a source or results table that is not a plain ``table`` or ``database.table``.

        The runner splices the source into ``FROM`` and the target into
        ``INSERT INTO``, so anything else could name a table function that reads
        from, or writes every detection to, another host. ``split`` is
        :func:`plain_source_name` for a source, whose name may carry ``-``.
        """
        if not isinstance(value, str):
            raise ValueError(f"Invalid {label}. It should be a table name, not {value!r}.")
        try:
            split(value)
        except ValueError as exc:
            raise ValueError(f"Invalid {label}: {exc}") from exc

    @staticmethod
    def _validate_customers(hunt_data: dict) -> list:
        """Validate customers list and return it."""
        customers = hunt_data.get("customers")
        if not customers or not isinstance(customers, list):
            raise ValueError("Customers list is missing or invalid.")
        return customers

    @staticmethod
    def _validate_rules(
        hunt_data: dict, rules_dir: str | Path, source_registry: Any = None
    ) -> None:
        """Validate rules configuration and the rule files the hunt runner reads."""
        rules = hunt_data.get("rules", [])
        for rule_info in rules:
            rule_name = rule_info.get("rule_name")
            fields = (
                ("source_table_name", plain_source_name),
                ("target_table_name", plain_table_name),
            )
            for field, split in fields:
                table = rule_info.get(field)
                if table is not None:
                    HuntValidator._validate_table_name(
                        f"'{field}' of rule '{rule_name}'", table, split=split
                    )
            rule_path = rule_file(rules_dir, str(rule_name or ""), ".yaml")
            if rule_path.is_file():
                HuntValidator.validate_rule_file(rule_path)
            else:
                # Rule file may be uploaded AFTER the hunt is drafted (create-then-add);
                # warn instead of blocking. The file is checked once it exists.
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
    def validate_rule_file(rule_path: Path) -> None:
        """Check a rule file reads as the YAML mapping the hunt runner compiles.

        :param rule_path: The rule's ``{name}.yaml``.
        :raises ValueError: If the file cannot be read as YAML, is not a mapping,
            or names a source that is not a plain table name; the runner drops
            such a rule.
        """
        try:
            payload = yaml_load(rule_path)
        except (YAMLError, UnicodeDecodeError, OSError) as e:
            raise ValueError(f"Rule file {rule_path} cannot be read as YAML: {e}") from e
        if not isinstance(payload, dict):
            raise ValueError(f"Rule file {rule_path} is not a YAML mapping")
        HuntValidator.validate_rule_source(payload, f"rule file {rule_path.name}")

    @staticmethod
    def validate_rule_source(payload: dict, label: str) -> None:
        """Check a rule document's ``source_db`` and ``source_table`` name a source table.

        :param payload: The rule document, as the hunt runner reads it.
        :param label: What the message names the rule as, such as ``rule file x.yaml``.
        :raises ValueError: If the pair is not a plain source table name; the runner
            drops such a rule.
        """
        source = rule_source(
            str(payload.get("source_db") or ""), str(payload.get("source_table") or "")
        )
        if source:
            HuntValidator._validate_table_name(
                f"source of {label}", source, split=plain_source_name
            )
