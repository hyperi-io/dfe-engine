import os
from typing import Dict, List, Tuple

from hyperi_pylib.logger import logger
from sigma.collection import SigmaCollection

from ..sigma.field_mapping_service import FieldMappingService
from ..sigma.sigma_backend_clickhouse import SqlBackend
from ..sigma.sigma_pipelines import SigmaPipeline
from ..yaml_utils import yaml_load


class SigmaRuleConverter:
    def __init__(
        self,
        args_dfe_package_file_path: str,
        input_directory: str,
        output_directory: str,
        dfe_root_log_path: str,
    ) -> None:
        """
        Initializes the SigmaRuleConverter with the input and output directories and DFE configuration file path.

        :param args_dfe_package_file_path: Path to the DFE package configuration file.
        :param input_directory: Directory to scan for Sigma rule files.
        :param output_directory: Directory to write the converted rules.
        :param dfe_root_log_path: Directory for logging.
        """
        self.input_directory = input_directory
        self.output_directory = output_directory
        self.args_dfe_package_file_path = args_dfe_package_file_path
        self._ensure_output_directory_exists()
        self.config = self._load_dfe_config()
        self.field_mapping_service = FieldMappingService(self.config)

    def _ensure_output_directory_exists(self) -> None:
        """Creates the output directory if it does not exist."""
        if not os.path.exists(self.output_directory):
            os.makedirs(self.output_directory)
            logger.debug(f"Output directory created: {self.output_directory}")

    def _load_dfe_config(self) -> dict:
        """Loads the DFE package configuration."""
        try:
            return yaml_load(self.args_dfe_package_file_path)
        except Exception as e:
            logger.error(f"Error loading DFE config: {e}")
            raise

    def _load_included_sigma_rules(self, include_path: str) -> dict:
        """
        Loads sigma rules from an included file.

        :param include_path: Path to the included sigma rules file
        :return: Dictionary containing the sigma rules
        """
        try:
            if include_path.startswith("./"):
                include_path = include_path[2:]

            full_path = os.path.abspath(include_path)
            if not os.path.exists(full_path):
                base_dir = os.path.dirname(os.path.abspath(self.args_dfe_package_file_path))
                full_path = os.path.abspath(os.path.join(base_dir, include_path))
            included_config = yaml_load(full_path)
            if not included_config or "sigma_rules" not in included_config:
                return {}
            rules_config = included_config["sigma_rules"]
            if "rules" in rules_config and isinstance(rules_config["rules"], dict):
                device_types = list(rules_config["rules"].keys())
                if device_types:
                    device_type = device_types[0]
                    rules_config["rules"] = rules_config["rules"][device_type]
                    if isinstance(rules_config["rules"], list):
                        for i, rule in enumerate(rules_config["rules"]):
                            if isinstance(rule, dict):
                                rule["path"] = f"{device_type}/{rule['path']}"
                            else:
                                rules_config["rules"][i] = f"{device_type}/{rule}"
                return rules_config
            return {}
        except Exception as e:
            logger.error(f"Error loading included sigma rules from {include_path}: {e}")
            return {}

    def _get_schema_sigma_rules(self, schema_config: dict) -> dict:
        """
        Gets sigma rules configuration, either directly from the config or from an included file.

        :param schema_config: Schema configuration dictionary
        :return: Dictionary containing the sigma rules configuration
        """
        if "include_sigma_rules" in schema_config:
            return self._load_included_sigma_rules(schema_config["include_sigma_rules"])
        return {}

    def _get_rule_dynamic_metadata(self, sigma_rules_config: dict, rule_path: str) -> dict:
        """
        Gets dynamic metadata for a specific rule.

        :param sigma_rules_config: Sigma rules configuration dictionary
        :param rule_path: Path to the rule file
        :return: Dictionary containing dynamic metadata for the rule
        """
        if not sigma_rules_config or "rules" not in sigma_rules_config:
            return {}

        for rule in sigma_rules_config["rules"]:
            if isinstance(rule, dict):
                if rule.get("path") == rule_path:
                    return rule.get("alert_dynamic_metadata", {})
            elif isinstance(rule, str) and rule == rule_path:
                return {}

        return {}

    def _extract_rule_metadata(self, rule: dict) -> dict:
        """
        Extracts metadata from a sigma rule.

        :param rule: Parsed sigma rule dictionary
        :return: Dictionary containing alert metadata
        """
        if isinstance(rule, str):
            try:
                import json

                rule = json.loads(rule)
            except (json.JSONDecodeError, TypeError) as e:
                logger.error(f"Failed to parse rule content as JSON: {e}")
                return {}

        metadata = {
            "alert_schedule": "smd",
            "alert_schedule_duration": "10mins",
            "alert_ratingtime_sla_applies": "true",
            "alert_framework": "MITRE ATT&CK",
        }

        level_map = {
            "critical": {"severity": "critical", "score": 90},
            "high": {"severity": "high", "score": 70},
            "medium": {"severity": "medium", "score": 50},
            "low": {"severity": "low", "score": 30},
        }
        level = rule.get("level", "medium").lower()
        if level in level_map:
            metadata["alert_severity"] = level_map[level]["severity"]
            metadata["alert_triage_score"] = level_map[level]["score"]

        if "title" in rule:
            metadata["alert_type"] = rule["title"]
        elif "tags" in rule and rule["tags"]:
            metadata["alert_type"] = (
                rule["tags"][0].replace("attack.", "").replace("_", " ").title()
            )

        if "description" in rule:
            desc = rule["description"]
            desc = (
                desc.replace("\\", "\\\\")
                .replace("'", "''")
                .replace("%", "%%")
                .replace("_", "\\_")
                .replace("\0", "")
                .replace("\b", "")
                .replace("\n", " ")
                .replace("\r", " ")
                .replace("\t", " ")
                .replace("\x1a", "")
            )
            metadata["alert_description"] = desc

        return metadata

    def _get_schema_mappings(
        self, schema_config: dict, rule_name: str
    ) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
        """
        Gets field mappings from schema configuration and global mappings.

        :param schema_config: Schema configuration dictionary
        :param rule_name: Name of the sigma rule
        :return: Tuple of (field mappings dictionary, schema metadata dictionary)
        """
        return self.field_mapping_service.get_schema_mappings(schema_config, rule_name)

    def convert(self, file_path: str, schema_config: dict) -> None:
        """
        Converts a single Sigma rule file to the Jinja2 format and writes it to the output directory.

        :param file_path: Path to the Sigma rule YAML file (or rule name in API mode).
        :param schema_config: Schema configuration dictionary
        """
        rule_name = os.path.basename(file_path)
        logger.info(f"Converting rule: {rule_name}")

        try:
            if not os.path.exists(file_path):
                logger.error(f"File not found: {file_path}")
                raise FileNotFoundError(f"File not found: {file_path}")

            rule = yaml_load(file_path)

            rel_path = os.path.relpath(file_path, self.input_directory)
            if not rel_path:
                logger.error(f"Could not get relative path for: {file_path}")
                return

            field_mappings, schema_metadata = self._get_schema_mappings(schema_config, rel_path)
            if not field_mappings:
                logger.warning("No field mappings found for schema")
                return

            source_fields = self.field_mapping_service.get_rule_source_fields(rule)

            schema_name = None
            if "name" in schema_config:
                schema_name = schema_config["name"]

            missing_mappings = self.field_mapping_service.validate_field_mappings(
                source_fields, field_mappings, schema_metadata, schema_name
            )

            if missing_mappings:
                logger.warning(
                    f"Rule '{rule_name}' has missing mappings for fields: {', '.join(missing_mappings)}"
                )

            alert_metadata = self._extract_rule_metadata(rule)
            sigma_rules_config = self._get_schema_sigma_rules(schema_config)
            dynamic_metadata = self._get_rule_dynamic_metadata(sigma_rules_config, rule_name)

            rule_specific_mappings = {}
            if "sigma_rules" in schema_config and rel_path in schema_config.get("sigma_rules", {}):
                rule_specific_mappings = schema_config["sigma_rules"][rel_path].get(
                    "alert_fields", {}
                )

            all_mappings = {**field_mappings, **rule_specific_mappings}

            pipeline_mappings = {}
            for k, v in all_mappings.items():
                if v is None:
                    continue
                if "," in str(v):
                    columns = [col.strip().replace(".", "_") for col in v.split(",")]
                    pipeline_mappings[k] = columns
                else:
                    pipeline_mappings[k] = v.replace(".", "_")

            matched_schema_info = {}

            for sigma_field in source_fields:
                if sigma_field in pipeline_mappings:
                    mapped_field = pipeline_mappings[sigma_field]

                    if isinstance(mapped_field, list):
                        for field in mapped_field:
                            if field in schema_metadata:
                                matched_schema_info[field] = schema_metadata[field]
                                if (
                                    schema_metadata[field]["type"] == "text"
                                    and schema_metadata[field]["index_type"] != "text_search"
                                ):
                                    logger.warning(
                                        f"Field {field} is text type but missing text_search index"
                                    )
                    else:
                        if mapped_field in schema_metadata:
                            matched_schema_info[mapped_field] = schema_metadata[mapped_field]
                            if (
                                schema_metadata[mapped_field]["type"] == "text"
                                and schema_metadata[mapped_field]["index_type"] != "text_search"
                            ):
                                logger.warning(
                                    f"Field {mapped_field} is text type but missing text_search index"
                                )

            pipeline_config = SigmaPipeline(field_mappings=pipeline_mappings)
            pipeline = pipeline_config.create_pipeline()

            backend = SqlBackend(
                pipeline,
                alert_metadata=alert_metadata,
                dynamic_metadata=dynamic_metadata,
                schema_metadata=matched_schema_info,
                field_mappings=all_mappings,
            )

            rules = SigmaCollection.load_ruleset([file_path])
            converted_rules = backend.convert(rules, output_format="full_alert")
            cleaned_rules = " ".join(
                converted_rules.replace("\n", " ").replace("\r", " ").split()
            )
            formatted_rules = self.format_rules(cleaned_rules)

            filename = os.path.splitext(os.path.basename(file_path))[0]
            output_file_path = os.path.join(self.output_directory, f"{filename}.jinja2")

            with open(output_file_path, "w") as output_file:
                output_file.write(formatted_rules)

            logger.info(f"Rule converted: {output_file_path}")

        except Exception as e:
            logger.error(f"Exception during conversion: {e}")
            raise

    def _optimize_schema_info(self, schema_metadata: dict) -> dict:
        """
        Analyzes schema metadata to provide optimization hints.

        :param schema_metadata: Original schema metadata
        :return: Dictionary with optimization information
        """
        optimized_info = {"schema_metadata": schema_metadata.copy(), "field_hints": {}}

        for field, info in schema_metadata.items():
            if info.get("use_case") == "dimension" and info["type"] == "string":
                optimized_info["field_hints"][field] = {"matching": "exact", "index_priority": 1}
            elif info["type"] == "text" and info.get("use_case") in ("fulltext", "text_search"):
                optimized_info["field_hints"][field] = {
                    "matching": "text_search",
                    "index_priority": 2,
                }
            elif info["type"] in ("integer", "float"):
                optimized_info["field_hints"][field] = {"matching": "numeric", "index_priority": 1}

        return optimized_info

    def _optimize_query(self, query: str, optimization_info: dict) -> str:
        """
        Post-processes the query for better performance.

        :param query: Original SQL query
        :param optimization_info: Optimization information
        :return: Optimized query
        """
        where_start = query.find("WHERE")
        if where_start == -1:
            return query

        before_where = query[:where_start]
        where_clause = query[where_start:]

        conditions = []
        current_condition = ""
        parentheses_count = 0

        for char in where_clause[6:]:
            current_condition += char
            if char == "(":
                parentheses_count += 1
            elif char == ")":
                parentheses_count -= 1
            elif char == " " and parentheses_count == 0:
                if current_condition.strip().upper() in ["AND", "OR"]:
                    current_condition = ""
                    continue
                conditions.append(current_condition.strip())
                current_condition = ""

        if current_condition.strip():
            conditions.append(current_condition.strip())

        def get_condition_priority(condition):
            if "=" in condition and "ILIKE" not in condition:
                return 1
            elif "ILIKE" in condition and not condition.split("ILIKE")[1].strip().startswith("'%"):
                return 2
            return 3

        sorted_conditions = sorted(conditions, key=get_condition_priority)
        optimized_where = "WHERE " + " AND ".join(sorted_conditions)

        return before_where + optimized_where

    @staticmethod
    def format_rules(rules: str) -> str:
        """
        Formats the converted rules for better readability.

        :param rules: The converted rules as a string.
        :return: Formatted rules string with proper SQL formatting.
        """
        statements = [stmt.strip() for stmt in rules.split(";") if stmt.strip()]

        formatted_statements = []
        for stmt in statements:
            parts = stmt.split(" FROM ")
            if len(parts) != 2:
                formatted_statements.append(stmt + ";")
                continue

            insert_select, from_where = parts

            if "INSERT INTO" in insert_select:
                insert_into, select_cols = insert_select.split("SELECT")

                insert_into = insert_into.replace("INSERT INTO", "INSERT INTO\n  ").strip()

                if "(" in insert_into:
                    cols_start = insert_into.find("(")
                    cols_end = insert_into.find(")")
                    if cols_start > -1 and cols_end > -1:
                        cols = insert_into[cols_start + 1 : cols_end].split(",")
                        formatted_cols = ",\n    ".join(col.strip() for col in cols)
                        insert_into = f"{insert_into[:cols_start]}(\n    {formatted_cols}\n  )"

                select_cols = select_cols.strip()
                cols = select_cols.split(",")
                formatted_select_cols = ",\n    ".join(col.strip() for col in cols)

                select_part = f"\nSELECT\n    {formatted_select_cols}"
            else:
                insert_into = ""
                select_part = insert_select

            from_where_parts = from_where.split(" WHERE ")
            from_part = f"\nFROM {from_where_parts[0].strip()}"

            where_part = ""
            if len(from_where_parts) > 1:
                where_clause = from_where_parts[1].strip()
                formatted_where = SigmaRuleConverter._format_where_clause(where_clause)
                where_part = f"\nWHERE\n{formatted_where}"

            formatted_stmt = f"{insert_into}{select_part}{from_part}{where_part};"
            formatted_statements.append(formatted_stmt)

        return "\n\n".join(formatted_statements)

    @staticmethod
    def _format_where_clause(where_clause: str) -> str:
        """
        Format a WHERE clause with proper indentation and handling of string literals.

        :param where_clause: The WHERE clause to format.
        :return: A properly formatted WHERE clause.
        """
        formatted = []
        indent_level = 6
        in_string = False
        string_delimiter = None
        i = 0
        current_line = " " * indent_level
        skip_next = False
        parenthesis_stack = []

        while i < len(where_clause):
            if skip_next:
                skip_next = False
                i += 1
                continue

            char = where_clause[i]

            if char in ["'", '"'] and (i == 0 or where_clause[i - 1] != "\\"):
                if not in_string:
                    in_string = True
                    string_delimiter = char
                    current_line += char
                elif char == string_delimiter:
                    if i > 0 and where_clause[i - 1] == "\\":
                        current_line += char
                    else:
                        in_string = False
                        current_line += char
                else:
                    current_line += char
            elif in_string:
                current_line += char
                if char == "\\" and i + 1 < len(where_clause):
                    current_line += where_clause[i + 1]
                    i += 1

            elif not in_string:
                if char == "(":
                    parenthesis_stack.append(indent_level)
                    indent_level += 2

                    if current_line.strip():
                        formatted.append(current_line)
                        current_line = " " * indent_level
                        current_line += char
                    else:
                        current_line += char

                elif char == ")":
                    if parenthesis_stack:
                        indent_level = parenthesis_stack.pop()

                    if current_line.strip():
                        current_line += char
                    else:
                        current_line = " " * indent_level + char

                    next_tokens = where_clause[i + 1 : i + 6].strip().upper()
                    if next_tokens.startswith("AND ") or next_tokens.startswith("OR "):
                        formatted.append(current_line)
                        current_line = " " * indent_level

                elif char == " ":
                    next_5 = where_clause[i : i + 6].upper()
                    if next_5.startswith(" AND "):
                        formatted.append(current_line)
                        current_line = " " * indent_level + "AND "
                        i += 4
                    elif next_5.startswith(" OR "):
                        formatted.append(current_line)
                        current_line = " " * indent_level + "OR "
                        i += 3
                    else:
                        current_line += char
                else:
                    current_line += char

            i += 1

        if current_line.strip():
            formatted.append(current_line)

        return "\n".join(formatted)

    def _get_rule_source_fields(self, rule: dict) -> List[str]:
        """
        Extracts source fields used in a sigma rule.

        :param rule: Parsed sigma rule dictionary
        :return: List of source field names used in the rule
        """
        return self.field_mapping_service.get_rule_source_fields(rule)

    def list_rules_by_schema(self) -> Dict[str, List[Dict[str, str]]]:
        """
        Lists all sigma rules mapped to each schema with their metadata.

        :return: Dictionary mapping schema names to lists of rule metadata
        """
        rules_by_schema = {}
        all_schemas = self.config.get("schemas", {})

        for schema_name, schema_config in all_schemas.items():
            sigma_rules_config = self._get_schema_sigma_rules(schema_config)
            if not sigma_rules_config:
                continue

            rules = []
            for rule_file in sigma_rules_config.get("rules", []):
                full_path = os.path.join(self.input_directory, rule_file)
                try:
                    rule = yaml_load(full_path)
                    rules.append(
                        {
                            "title": rule.get("title", ""),
                            "id": rule.get("id", ""),
                            "name": rule.get("title", "").lower().replace(" ", "_"),
                            "path": rule_file,
                            "level": rule.get("level", ""),
                            "logsource": rule.get("logsource", {}),
                            "description": rule.get("description", ""),
                            "tags": rule.get("tags", []),
                            "source_fields": self._get_rule_source_fields(rule),
                        }
                    )
                except Exception as e:
                    logger.error(f"Error reading rule {rule_file}: {e}")

            if rules:
                rules_by_schema[schema_name] = rules

        return rules_by_schema

    def generate_sigma_rules_clickhouse(self) -> None:
        """
        Scans the input directory for Sigma rule files and converts them.
        If input_directory points to a file, converts just that file.
        """
        self._ensure_output_directory_exists()
        all_schemas = self.config.get("schemas", {})
        conversion_stats = {}

        if os.path.isfile(self.input_directory):
            single_file = self.input_directory
            os.path.basename(single_file)

            for schema_name, schema_config in all_schemas.items():
                if "include_sigma_rules" not in schema_config:
                    continue

                schema_output_dir = os.path.join(self.output_directory, schema_name)
                if not os.path.exists(schema_output_dir):
                    os.makedirs(schema_output_dir)

                logger.info(f"Processing single rule for schema: {schema_name}")
                original_output_dir = self.output_directory
                self.output_directory = schema_output_dir
                self.convert(single_file, schema_config)
                self.output_directory = original_output_dir
                conversion_stats[schema_name] = 1
            return

        for schema_name, schema_config in all_schemas.items():
            if "include_sigma_rules" not in schema_config:
                continue

            sigma_rules_config = self._get_schema_sigma_rules(schema_config)
            if not sigma_rules_config or "rules" not in sigma_rules_config:
                logger.info(f"No rules found for schema: {schema_name}")
                continue

            logger.info(f"Processing rules for schema: {schema_name}")
            schema_output_dir = os.path.join(self.output_directory, schema_name)
            if not os.path.exists(schema_output_dir):
                os.makedirs(schema_output_dir)

            for rule in sigma_rules_config["rules"]:
                rule_path = rule.get("path") if isinstance(rule, dict) else rule
                if not rule_path:
                    continue

                file_path = os.path.join(self.input_directory, rule_path)
                if not os.path.isfile(file_path):
                    file_path = os.path.join(self.input_directory, rule_path)

                if os.path.isfile(file_path):
                    try:
                        original_output_dir = self.output_directory
                        self.output_directory = schema_output_dir
                        self.convert(file_path, schema_config)
                        self.output_directory = original_output_dir
                        conversion_stats[schema_name] = conversion_stats.get(schema_name, 0) + 1
                    except Exception as e:
                        logger.error(f"Error converting rule {file_path}: {e}")
                else:
                    logger.error(f"Rule file not found: {file_path}")

        for schema_name, count in conversion_stats.items():
            logger.info(f"Total rules converted for schema '{schema_name}': {count}")
