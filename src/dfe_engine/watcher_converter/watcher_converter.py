import pandas as pd
import logging
import re
from typing import Optional
import yaml
import os
from .watcher_parser import map_device_name


SQL_TEMPLATE = """
    INSERT INTO {org_id}.{target_table}
    (
        alert_description,
        alert_framework,
        alert_ratingtime_sla_applies,
        alert_rule_name,
        alert_schedule,
        alert_schedule_duration,
        alert_severity,
        alert_triage_score,
        alert_type,
        detected_time,
        event_hash,
        logoriginal,
        org_id,
        source_table,
        tactic_id,
        tactic_name,
        tactic_reference,
        technique_id,
        technique_name,
        technique_reference,
        timestamp,
        timestamp_finalise,
        timestamp_load,
        timestamp_received{extension_alert_select_field_names}
    ){cte_with_clause}
    SELECT
        '{alert_description}',
        '{alert_framework}',
        '{alert_ratingtime_sla_applies}',
        '{alert_rule_name}',
        '{alert_schedule}',
        '{alert_schedule_duration}',
        '{alert_severity}',
        {alert_triage_score},
        '{alert_type}',
        {detected_time},
        {event_hash},
        {logoriginal},
        '{org_id}',
        '{source_table}',
        '{tactic_id}',
        '{tactic_name}',
        '{tactic_reference}',
        '{technique_id}',
        '{technique_name}',
        '{technique_reference}',
        {timestamp},
        {timestamp_finalise},
        {detected_time},
        {timestamp_received}{extension_alert_select_field_names}
    FROM
        {from_table}{condition}
    """


class WatcherConverter:
    def __init__(
        self,
        output_directory: str,
        sort_by_device: bool = False,
        logger: Optional[logging.Logger] = None,
    ):
        self.output_directory = output_directory
        self.sort_by_device = sort_by_device
        self.logger = logger if logger else self._setup_default_logger()

    def _setup_default_logger(self) -> logging.Logger:
        """
        Sets up a default logger if one isn't provided.
        """
        logger = logging.getLogger("WatcherConverter")
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            ch = logging.StreamHandler()
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            ch.setFormatter(formatter)
            logger.addHandler(ch)
        return logger

    def _write_to_yaml(self, sql_insert: str, filename: str):
        """
        Writes the SQL insert statement to a yaml file.

        :param sql_insert: String containing converted and formatted watchers.
        :param filename: Name of the file you want to write the converted SQL to.
        """
        # Ensure output directory exists
        os.makedirs(
            self.output_directory, exist_ok=True
        )  # Create the directory if it doesn't exist
        filepath = os.path.join(self.output_directory, f"{filename}.yaml")
        formatted_yaml = {
            "sql_statement": sql_insert  # Assuming sql_insert is your formatted SQL string
        }
        with open(filepath, "w") as file:
            yaml.dump(
                formatted_yaml,
                file,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                indent=4,
                Dumper=yaml.SafeDumper,
            )
        self.logger.info(f"SQL statement written to {filepath}")

    def _write_to_file(self, sql_insert: str, filename: str):
        """
        Writes the SQL insert statement to a text file.

        :param sql_insert: String containing converted and formatted watchers.
        :param filename: Name of the file you want to write the converted SQL to.
        """
        # Ensure output directory exists
        os.makedirs(self.output_directory, exist_ok=True)
        if "/" in filename:
            os.makedirs(
                os.path.join(self.output_directory, "/".join(filename.split("/")[:-1])),
                exist_ok=True,
            )
        filepath = os.path.join(
            self.output_directory, f"{filename.replace('.json', '')}.jinja2"
        )
        with open(filepath, "w") as file:
            file.write(sql_insert)
        self.logger.info(f"SQL statement written to {filepath}")

    def _get_where_condition(self, row):
        """Safely extract where condition from row data."""
        where_clause = row.get("where_clause")
        if isinstance(where_clause, dict):
            return where_clause.get("where", "1=1")
        elif isinstance(where_clause, str):
            return where_clause
        else:
            return "1=1"

    def _set_source_table_and_check_device_types(self, row):
        device_types = row.get("device_types", set())

        if not device_types:
            self.logger.warning("No device types found for detection.")
            return None  # or some default value you would prefer

        source_table = "{{source_table_name}}"  # list(device_types)[0]

        # if len(device_types) > 1:
        #     self.logger.warning(f"Multiple device types ({', '.join(device_types)}) found in this detection. Using top device type as source table [{source_table}]")

        return source_table

    def _generate_cte_join(self, source_name, cte_name, cte_where_condition):
        return f""" AS {source_name}
        JOIN (
            SELECT
                event_hash
            FROM
                {cte_name}
            WHERE
                {cte_where_condition}
        ) AS filtered_hashes
        ON {source_name}.event_hash = filtered_hashes.event_hash"""

    def _generate_and_write_sql_inserts(self, df: pd.DataFrame):
        """
        Generates SQL insert statements from DataFrame rows and writes them to files.

        :param df: DataFrame containing watcher configuration data.
        """
        failed_watchers = []
        successful_watchers = []
        
        for index, row in df.iterrows():
            try:
                self.logger.info(f" values for sql template generation \n\n{row}\n\n")

                # Format and extract all event specific fields
                select_fields = row.get("select_fields", [])
                
                # Handle different select_fields structures safely
                extension_alert_select_field_names = ""
                if select_fields:
                    if isinstance(select_fields, list) and len(select_fields) > 0:
                        if isinstance(select_fields[0], list):
                            # List of lists
                            field_list = select_fields[0]
                        else:
                            # Simple list
                            field_list = select_fields
                        
                        if field_list and all(isinstance(f, str) for f in field_list):
                            prefix_required = ",\n" + " " * 8
                            extension_alert_select_field_names = (
                                prefix_required + f",\n{' ' * 8}".join(field_list)
                            )
                
                org_id = "{{org_id}}"
                source_table = self._set_source_table_and_check_device_types(row)

                cte_with_clause_value = row.get("cte_with_clause") or ""
                cte_table_name_value = row.get("cte_table_name") or ""
                cte_where_condition_value = row.get("cte_where_condition") or ""

                sql_insert = SQL_TEMPLATE.format(
                    org_id=org_id,
                    target_table="{{target_table_name}}",
                    # INSERT INTO fields
                    extension_alert_select_field_names=extension_alert_select_field_names,
                    # CTE related fields
                    cte_with_clause=""
                    if len(row.get("aggs", "")) == 0
                    else f"\n{' ' * 4}"
                    + cte_with_clause_value
                    .format(
                        org_id=org_id,
                        source_table=f"{self._set_source_table_and_check_device_types(row)}",
                        condition=cte_where_condition_value,
                        cte_table_name=cte_table_name_value,
                    )
                    .strip(),
                    # SELECT fields
                    alert_description=row.get("threat.detection_description", ""),
                    alert_framework=row.get("threat.framework", ""),
                    alert_ratingtime_sla_applies=str(
                        row.get("threat.ratingtime_sla_applies", "false")
                    ).lower(),
                    alert_rule_name=row.get("watcher_id", ""),
                    alert_schedule=row.get("detection_type", ""),
                    alert_schedule_duration=row.get("schedule_duration", [""])[0],
                    alert_severity=row.get("threat.severity", ""),
                    alert_triage_score=str(row.get("threat.triage_score", "")),
                    alert_type=row.get("title", ""),
                    detected_time="NOW()",
                    event_hash="event_hash",
                    logoriginal="logoriginal",
                    source_table=source_table,
                    tactic_id=row.get("threat.tactic.id", ""),
                    tactic_name=row.get("threat.tactic.name", ""),
                    tactic_reference=row.get("threat.tactic.reference", ""),
                    technique_id=row.get("threat.technique.id", ""),
                    technique_name=row.get("threat.technique.name", ""),
                    technique_reference=row.get("threat.technique.reference", ""),
                    timestamp="timestamp",
                    timestamp_finalise="timestamp_finalise",
                    timestamp_received="timestamp_received",
                    # FROM field
                    from_table=f"{org_id}.{source_table}{'' if len(row.get('aggs', '')) == 0 else self._generate_cte_join(source_table, cte_table_name_value, cte_where_condition_value)}",
                    # CONDITION field
                    condition="\nWHERE\n"
                    + " " * 8
                    + "{timestamp_condition} AND "
                    + self._get_where_condition(row)
                    if (len(row.get("aggs", "")) == 0)
                    else "",
                )

                sql_insert = re.sub(
                    r"^    ", "", sql_insert, flags=re.MULTILINE
                ).strip()

                # Write the SQL insert statement to a YAML file named after the row's filename column
                unique_device_types = []
                for device in row["device_types"]:
                    if map_device_name(device) not in unique_device_types:
                        unique_device_types.append(map_device_name(device))
                if len(unique_device_types) > 1:
                    self.logger.warning(
                        f"There are more than one device types in {(row.get('watcher_id', ''),)}. Will only use {unique_device_types[0]}."
                    )
                device_name = unique_device_types[0]
                if self.sort_by_device:
                    self._write_to_file(
                        sql_insert, device_name + "/rules/" + row["name"]
                    )
                else:
                    self._write_to_file(sql_insert, row["name"])
                
                successful_watchers.append(row.get('name', row.get('filename', 'unknown')))

            except Exception as e:
                watcher_name = row.get('name', row.get('filename', 'unknown'))
                self.logger.error(
                    f"Error processing watcher {watcher_name}: {e}",
                    exc_info=True,
                )
                failed_watchers.append({
                    'name': watcher_name,
                    'error': str(e)
                })
                # Continue processing other watchers instead of failing completely
                continue
        
        # Report summary
        self.logger.info(f"Watcher conversion summary: {len(successful_watchers)} successful, {len(failed_watchers)} failed")
        if successful_watchers:
            self.logger.info(f"Successfully converted: {successful_watchers}")
        if failed_watchers:
            self.logger.error(f"Failed to convert: {[f['name'] for f in failed_watchers]}")
            for failed in failed_watchers:
                self.logger.error(f"  - {failed['name']}: {failed['error']}")

    def convert_and_write_sql(self, df: pd.DataFrame, org_id: str = None):
        """
        Public method to convert DataFrame rows to SQL insert statements and write each to a YAML file.

        :param df: DataFrame containing watcher configuration data.
        :param org_id: Optional organization ID to inject into dataframe for template rendering.
        """
        if org_id is not None:
            df = df.copy()
            df['org_id'] = org_id
        self._generate_and_write_sql_inserts(df)
