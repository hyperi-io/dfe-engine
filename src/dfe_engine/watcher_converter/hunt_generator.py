import os
import re

import pandas as pd
from hs_lib.logger import logger

HUNT_TEMPLATE = """
    name: "{name}"
    cron: "{cron}"
    log_buffer: {log_buffer}
    global_target_table_name: {global_target_table_name}
    customers:
    {customers}

    rules:
    {rules}

    customer_filters:
    {customer_filters}

    # Example:
    # name: "First Ten Windows Audit Hunt"
    # cron: "* * * * *" # Run every minute
    # log_buffer: 60 
    # global_target_table_name: logs_alerts
    # customers:
    #   - detectionlab

    # rules:
    #   - rule_name: win_account_backdoor_dcsync_rights      
    #   - rule_name: win_ad_cert_services_priv_escalation    # SMD | Runs every 10 mins
    #   - rule_name: win_ad_cert_services_templates          # SMD | Runs every 10 mins
    #   - rule_name: win_alert_hacktool_ruler                # SMD | Runs every 10 mins
    #   - rule_name: win_alert_lsass_access                  # SMD | Runs every 10 mins
    #     source_table_name: logs_nxlog_windows
    #     target_table_name: logs_alerts
    #   - rule_name: win_appinstaller_spawned                # SMD | Runs every 10 mins
    #     target_table_name: logs_alerts  
    #   - rule_name: win_apt_silence_downloader              # SMD | Runs every 10 mins
    #     target_table_name: logs_alerts  
    #   - rule_name: win_archive_collected_data_via_utility  # SMD | Runs every 10 mins
    #     target_table_name: logs_alerts  

    # customer_filters:
    #   detectionlab:
    #     rules:
    #       - name: win_account_backdoor_dcsync_rights
    #         filter_clause: ""
    #       - name: win_ad_cert_services_priv_escalation
    #         filter_clause: ""
    #       - name: win_ad_cert_services_templates
    #         filter_clause: ""
    #       - name: win_alert_hacktool_ruler
    #         filter_clause: ""
    #       - name: win_alert_lsass_access
    #         filter_clause: ""
    #       - name: win_appinstaller_spawned
    #         filter_clause: ""
    #       - name: win_apt_silence_downloader
    #         filter_clause: ""
    #       - name: win_archive_collected_data_via_utility
    #         filter_clause: ""
"""


class HuntGenerator:
    def __init__(self, output_directory: str):
        self.output_directory = output_directory

    def _map_schedule_to_cron(self, schedule: str):
        if "m" in schedule:
            return f"*/{schedule[:-1]} * * * *"
        elif "h" in schedule:
            return f"0 */{schedule[:-1]} * * *"
        else:
            raise ValueError(
                f"Invalid schedule format: {schedule}. Expected format like '5m' or '1h'"
            )

    def _create_new_hunt_config(
        self,
        device: str,
        schedule: str,
        customers: list,
        rule_name: str,
        whitelisting: str,
        hunt_filepath: str,
    ):
        hunt_config = HUNT_TEMPLATE.format(
            name=f"{device.replace('_', ' ').lower().title()} {schedule} Hunt",
            cron=f"{self._map_schedule_to_cron(schedule)}",
            log_buffer="60",
            global_target_table_name="logs_alerts",
            customers="\n".join(["  - " + customer for customer in customers]),
            rules=f"  - {rule_name}",
            customer_filters=whitelisting,
        )

        hunt_config = re.sub(r"^    ", "", hunt_config, flags=re.MULTILINE).strip()

        logger.info(hunt_config)

    def _generate_and_write_hunt_configs(self, df: pd.DataFrame):
        """
        Generates hunt configurations from DataFrame rows and writes them to files.

        :param df: DataFrame containing watcher configuration data.
        """
        for _index, row in df.iterrows():
            try:
                rule_name = row.get("name")
                device_types = row.get("device_types")
                schedule_duration = row.get("schedule_duration")
                if len(device_types) == 0 or len(schedule_duration) == 0:
                    raise ValueError(
                        f"Missing device_types or schedule_duration for rule: {rule_name}"
                    )

                device_prefix = "_".join(device for device in device_types)
                schedule_prefix = schedule_duration[0]
                hunt_filename = f"{device_prefix}-{schedule_prefix}-hunt.yaml"
                hunt_filepath = os.path.join(
                    self.output_directory, device_prefix, "hunts", hunt_filename
                )

                customers = row.get("device_customer_map")[device_prefix]
                whitelisting = "\n".join(
                    [
                        f"{' ' * 2}{key}:\n{' ' * 8}rules:\n{' ' * 10}- {rule_name}\n{' ' * 12}filter_clause: {value}"
                        for key, value in row.get("where_clause")["customers"].items()
                    ]
                )

                logger.error(type(whitelisting))

                if not (os.path.exists(hunt_filepath)):
                    self._create_new_hunt_config(
                        device_prefix,
                        schedule_duration[0],
                        customers,
                        rule_name,
                        whitelisting,
                        hunt_filepath,
                    )
                else:
                    self._check_existing_config(hunt_filepath)
                    # Need to check customers line up

            except Exception as e:
                logger.error(
                    f"Error processing watcher {row.get('filename', 'unknown')}: {e}",
                    exc_info=True,
                )

    def generate_and_write_yaml(self, df: pd.DataFrame):
        """
        Public method to convert DataFrame rows to hunt configurations and write each to a YAML file.

        :param df: DataFrame containing watcher configuration data.
        """
        self._generate_and_write_hunt_configs(df)
