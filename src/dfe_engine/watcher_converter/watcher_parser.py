import os
import json
import pandas as pd
import re
import logging
import glob
from typing import List, Dict, Any, Set
from copy import deepcopy
from . import cte_templates

warn_on_file = [
    # The following are TEMPORARILY WHITELISTED to be fixed at a later date:
    "f5_cve-2021-22992.json",  # Uses an exist on an int type (does not work) - Need to check type and cast
    "f5_cve-2021-44228.json",  # Uses a regex on an int type (does not work) - Need to check type and cast
    "proofpoint_ondemand_malware_detection.json",  # Needs to be an int for range analysis: email_malware_score
    "proofpoint_ondemand_susp_email_allowed.json",  # Needs to be an int for range analysis: email_spam_score
    "proofpoint_tap_targeted_phish_probable.json",  # Needs to be an int for range analysis: email_phish_score
    "asa_ips_detection.json",  # Needs to be an int for range analysis: event_category
    "dt_anomaly_detection.json",  # Needs to be an int for range analysis: threat_risk_score
    "microfocus_niqidm_rule_alerted.json",  # Needs to be an int for range analysis: scan_num_infected
    "microfocus_priority_alert.json",  # Needs to be an int for range analysis: scan_num_infected
    "endeavour_dt_priority_detection_O365.json",  # Needs to be an int for range analysis: threat_risk_score
    "endeavour_dt_priority_detection_taxii_ioc_alert.json",  # Needs to be an int for range analysis: threat_risk_score
    "proofpoint_ondemand_threat_detection.json",  # Uses an exist on an int type (does not work) - Need to check type and cast
    "endeavour_dt_priority_detection.json",  # Needs to be an int for range analysis: threat_risk_score
    "proofpoint_ondemand_phishing_detection.json",  # Uses an exist on an int type (does not work) - Need to check type and cast
    # The following contain script which will require manual conversion:
    "asa_firepower_user_agent_overflow.json",
    "aws_waf_user_agent_overflow.json",
    "azure_active_directory_user_agent_overflow.json",
    "azure_ad_sign_on_user_agent_overflow.json",
    "azure_infrastructure_user_agent_overflow.json",
    "barracuda_waf_user_agent_overflow.json",
    "checkpoint_opseclea_user_agent_overflow.json",
    "cisco_meraki_user_agent_overflow.json",
    "esx_user_agent_overflow.json",
    "f5_user_agent_overflow.json",
    "forcepoint_user_agent_overflow.json",
    "fortigate_IPS_detection.json",
    "fortigate_user_agent_overflow.json",
    "hparuba_user_agent_overflow.json",
    "imp_user_agent_overflow.json",
    "linux_bufferoverflow_attempt.json",
    "linux_user_agent_overflow.json",
    "mcafee_wg_user_agent_overflow.json",
    "netscaler_user_agent_overflow.json",
    "netskope_cloud_exchange_user_agent_overflow.json",
    "O365_user_agent_overflow.json",
    "okta_user_agent_overflow.json",
    "panos_user_agent_overflow.json",
    "pulsesecure_user_agent_overflow.json",
    "symantec_proxysg_user_agent_overflow.json",
    "umb_user_agent_overflow.json",
    "win_dns_malicious_domain.json",
    "win_kerberoasting.json",
    "win_user_agent_overflow.json",
    "workday_user_agent_overflow.json",
    "zscaler_user_agent_overflow.json",
    "dairyfarm_win_kerberoasting.json",  # in es_watchers/dairyfarm_secmon (customer specific)
    "rbnz-payments_win_dns_malicious_domain.json",  # in es_watchers/rbnz-payments_dsl_secmon (customer specific)
    "rbnz_win_kerberoasting.json",  # in es_watchers/rbnz_dsl_secmon
    "network_cve-2021-44228_ldap_outbound.json",
    # The following have a unique schedule format:
    "vec_azure_sentinel_medium_susp_rdp_OutofHours.json",
    "swick_win_account_creation_officehours.json",
    "swick_win_account_creation_outofhours.json",
    "ttline_win_OutOfHours_account_unlock.json",
    "tomss_linux_authentication_outside_business_hours.json",
    "tomss_linux_changes_to_otp.json",
    "tomss_linux_user_created_outside_business_hours.json",
    "ttline_win_brute_force_single_hostname_multiple_usernames_1.json",
    "ttline_win_brute_force_single_hostname_multiple_usernames_2.json",
    "ttline_win_OutOfHours_lockout.json",
    "vodafonenz_aws_symbee_admin_successful_login.json",
    # The following have a custom aggregation format:
    "fortigate_password_spraying_allow.json",  # uses 5 dataframe rows with 2 cardinality fields
    "rbnz-payments_fortigate_password_spraying_allow.json",  # uses 5 dataframe rows with 2 cardinality fields
    "tomss_linux_topcat_brute_force.json",  # uses 1 aggregation
    "wise_win_brute_force_multiple_hostname.json",  # uses 1 aggregation
    # The following have a minimum_should_match > 1 that != number of should conditions
    "fortigate_threat_detection.json",  # minimum_should_match = 2, should conditions = 5
    "netscaler_cve-2022-27518.json",  # minimum_should_match = 5, should_conditions = 7
    # The following are in use by non-migrated customers
    "aws_securityhub_gd_effects_exposure.json",  # only used by 'rbnz'
    "azure_sentinel_medium_susp_rdp.json",  # only used by 'chisholm' and 'tomss'
    "box_abnormal_activity_rule.json",  # only used by 'rbnz'
    "box_abnormally_high_file_upload.json",  # only used by 'rbnz'
    "box_admin_role_change.json",  # only used by 'rbnz'
    "box_anomalous_admin_login.json",  # only used by 'rbnz'
    "box_compliance_violations.json",  # only used by 'rbnz'
    "box_concurrent_access.json",  # only used by 'rbnz'
    "box_encryption_circumvention.json",  # only used by 'rbnz'
    "box_geographically_improbable_access.json",  # only used by 'rbnz'
    "box_multiple_alerts.json",  # only used by 'rbnz'
    "box_multiple_collaborations_blocked.json",  # only used by 'rbnz'
    "box_password_spraying_attempt.json",  # only used by 'rbnz'
    "box_potential_ransomware.json",  # only used by 'rbnz'
    "box_priority_detection.json",  # only used by 'rbnz'
    "box_sensitive_file_paths.json",  # only used by 'rbnz'
    "box_shield_anomalous_downloads.json",  # only used by 'rbnz'
    "box_shield_malware_in_file.json",  # only used by 'rbnz'
    "box_shield_suspicious_locations.json",  # only used by 'rbnz'
    "box_shield_suspicious_sessions.json",  # only used by 'rbnz'
    "box_suspicious_country_admin_login.json",  # only used by 'rbnz'
    "cisco_acs_anomalous_auth_method.json",  # only used by 'vodafonenz'
    "cisco_acs_anomalous_cert_activity.json",  # only used by 'vodafonenz'
    "cisco_acs_anomalous_saml_identityprovider.json",  # only used by 'vodafonenz'
    "cisco_acs_brute_force_single_username.json",  # only used by 'vodafonenz'
    "cisco_acs_cryptographic_failed_alert.json",  # only used by 'vodafonenz'
    "cisco_acs_ers_threat_events.json",  # only used by 'vodafonenz'
    "cisco_acs_invalid_input.json",  # only used by 'vodafonenz'
    "cisco_acs_logging_disruption.json",  # only used by 'vodafonenz'
    "cisco_acs_offline_forest.json",  # only used by 'vodafonenz'
    "cisco_acs_priority_detection.json",  # only used by 'vodafonenz'
    "cisco_acs_protocol_downgrade_attack.json",  # only used by 'vodafonenz'
    "cisco_acs_susp_admin_activity.json",  # only used by 'vodafonenz'
    "cisco_acs_susp_dns_error.json",  # only used by 'vodafonenz'
    "cisco_acs_threat_events.json",  # only used by 'vodafonenz'
    "cisco_acs_threat_matches.json",  # only used by 'vodafonenz'
    "cisco_dna_medium_severity_detection.json",  # only used by 'wknzta'
    "cisco_dna_priority_detection.json",  # only used by 'wknzta'
    "cisco_dna_threat_matches.json",  # only used by 'wknzta'
    "cisco_sna_anomaly.json",  # only used by 'wknzta'
    "cisco_sna_auditlogger_login_success.json",  # only used by 'wknzta'
    "cisco_sna_brute_force_single_user_multiple_ips.json",  # only used by 'wknzta'
    "cisco_sna_brute_force_single_user_single_ip.json",  # only used by 'wknzta'
    "cisco_sna_medium_severity_detection.json",  # only used by 'wknzta'
    "cisco_sna_priority_detection.json",  # only used by 'wknzta'
    "cisco_sna_threat_match_multiple_outgoing.json",  # only used by 'wknzta'
    "cisco_sna_threat_matches.json",  # only used by 'wknzta'
    "cloudflare_cve-2022-22965.json",  # only used by 'rbnz'
    "cloudflare_exploit_cross-site-scripting.json",  # only used by 'rbnz'
    "cloudflare_exploit_directory_traversal.json",  # only used by 'rbnz'
    "cloudflare_JNDIExploit_pattern.json",  # only used by 'rbnz'
    "cloudflare_malware_rce_coldfusion.json",  # only used by 'rbnz'
    "cloudflare_sunburst_masquerading.json",  # only used by 'rbnz'
    "cloudflare_threat_allowed.json",  # only used by 'rbnz'
    "cloudflare_threat_match_multiple_outgoing.json",  # only used by 'rbnz'
    "cloudflare_threat_matches.json",  # only used by 'rbnz'
    "cloudflare_tor_onion.json",  # only used by 'rbnz'
    "genesys_threat_matches.json",  # only used by 'vodafonenz'
    "juniper_json_threat_matches.json",  # only used by 'rbnz'
    "microsoft_defender_for_cloud_medium_severity_detection.json",  # only used by 'nzhg'
    "microsoft_defender_for_cloud_multiple_low_sev.json",  # only used by 'nzhg'
    "microsoft_defender_for_cloud_priority_detection.json",  # only used by 'nzhg'
    "nspcc_win_brute_force_account_lockouts.json",  # only used by 'nspcc'
    "oracle_ilom_account_creation.json",  # only used by 'rbnzpayments'
    "oracle_ilom_service_processor_reset.json",  # only used by 'rbnzpayments'
    "oracle_ilom_session_user_deletion.json",  # only used by 'rbnzpayments'
    "panos_prisma_bruteforce_globalprotect_single_user_single_ip.json",  # only used by 'rbnz'
    "panos_prisma_exploit_cross-site-scripting.json",  # only used by 'rbnz'
    "panos_prisma_exploit_directory_traversal.json",  # only used by 'rbnz'
    "panos_prisma_file_threat.json",  # only used by 'rbnz'
    "panos_prisma_malware_rce_coldfusion.json",  # only used by 'rbnz'
    "panos_prisma_malware_sunburst_masquerading.json",  # only used by 'rbnz'
    "panos_prisma_network_threat.json",  # only used by 'rbnz'
    "panos_prisma_spyware_threat.json",  # only used by 'rbnz'
    "panos_prisma_susp_port_scanning.json",  # only used by 'rbnz'
    "panos_prisma_susp_smb_activity.json",  # only used by 'rbnz'
    "panos_prisma_threat_matches.json",  # only used by 'rbnz'
    "panos_prisma_tor_onion.json",  # only used by 'rbnz'
    "panos_prisma_vulnerability_threat.json",  # only used by 'rbnz'
    "panos_prisma_wildfire_threat.json",  # only used by 'rbnz'
    "rbnz_azure_infrastructure_suspicious_country.json",  # only used by 'rbnz'
    "rbnz_panos_cve_2024_3400_threat_malware_match.json",  # only used by 'rbnz'
    "rbnz_searchlight_medium_and_low_sev_detection.json",  # only used by 'rbnz'
    "rbnz_snowflake_create_standard_user.json",  # only used by 'rbnz'
    "rbnz_snowflake_user_successful_login.json",  # only used by 'rbnz'
    "rbnz_win_kerberoasting.json",  # only used by 'rbnz'. Uses 'script' block. Will require manual conversion
    "rbnz-payments_f5_susp_host_connections.json",  # only used by 'rbnzpayments'
    "rbnz-payments_f5_susp_port_connections.json",  # only used by 'rbnzpayments'
    "rbnz-payments_f5_susp_smb_connections.json",  # only used by 'rbnzpayments'
    "rbnz-payments_f5_susp_smb_recon.json",  # only used by 'rbnzpayments'
    "rbnz-payments_fortigate_brute_force_single_username.json",  # only used by 'rbnzpayments'
    "rbnz-payments_fortigate_malware_rce_coldfusion.json",  # only used by 'rbnzpayments'
    "rbnz-payments_fortigate_password_spraying_allow.json",  # only used by 'rbnzpayments'. Uses a custom aggregation format (5 rows with 2 cardinality fields)
    "rbnz-payments_fortigate_susp_host_connections.json",  # only used by 'rbnzpayments'
    "rbnz-payments_fortigate_susp_port_connections.json",  # only used by 'rbnzpayments'
    "rbnz-payments_fortigate_susp_smb_connections.json",  # only used by 'rbnzpayments'
    "rbnz-payments_fortigate_susp_smb_recon.json",  # only used by 'rbnzpayments'
    "rbnz-payments_rsa_authmgr_brute_force.json",  # only used by 'rbnzpayments'
    "rbnz-payments_swift_alliance_successful_login.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_brute_force_account_lockouts.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_brute_force_single_hostname_multiple_usernames.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_brute_force_single_hostname_single_username.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_brute_force_single_username_multiple_hostnames.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_brute_force_single_username_multiple_ip.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_conti_eventviewer_logs_via_powershell.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_conti_install_linux_via_powershell.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_conti_lsass_dump_via_process_creation.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_dns_malicious_domain.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_domain_group_membership.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_domain_group_modified.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_domain_group_policy_change.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_domain_policy_change.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_domain_user_modified.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_local_user_login_failure.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_restricted_host_failed_login.json",  # only used by 'rbnzpayments'
    "rbnz-payments_win_susp_admin_location.json",  # only used by 'rbnzpayments'
    "redshield_exploit_cross-site-scripting.json",  # only used by 'tomss'
    "redshield_exploit_directory_traversal.json",  # only used by 'tomss'
    "rsa_authmgr_brute_force.json",  # only used by 'vodafonenz' and 'rbnzpayments'
    "rsa_authmgr_brute_force_single_ip_multiple_users.json",  # only used by 'vodafonenz' and 'rbnzpayments'
    "rsa_authmgr_dos.json",  # only used by 'vodafonenz' and 'rbnzpayments'
    "rsa_authmgr_impossible_travel.json",  # only used by 'vodafonenz' and 'rbnzpayments'
    "searchlight_low_sev_detection.json",  # only used by 'rbnz'
    "searchlight_medium_detection.json",  # only used by 'rbnz'
    "searchlight_medium_prioirty_alert_brand_protection.json",  # only used by 'rbnz'
    "searchlight_priority_detection.json",  # only used by 'rbnz'
    "secureit_threat_matches.json",  # only used by 'rbnz'
    "snowflake_change_permission.json",  # only used by 'rbnz'
    "snowflake_create_privileged_user.json",  # only used by 'rbnz'
    "snowflake_unusual_failed_login_singleip_multiple_users.json",  # only used by 'rbnz'
    "snowflake_unusual_failed_login_singleip_single_user.json",  # only used by 'rbnz'
    "swift_alliance_brute_force.json",  # only used by 'rbnzpayments'
    "swift_alliance_new_operator.json",  # only used by 'rbnzpayments'
    "swift_alliance_priority_detection.json",  # only used by 'rbnzpayments'
    "tacacs_brute_force_failed_logins.json",  # only used by 'vodafonenz'
    "tacacs_incorrect_credentials.json",  # only used by 'vodafonenz'
    "tomss_azure_sentinel_alerts.json",  # only used by 'tomss'
    "tomss_linux_abnormal_process_end.json",  # only used by 'tomss'
    "tomss_linux_account_creation.json",  # only used by 'tomss'
    "tomss_linux_admin_failed_login.json",  # only used by 'tomss'
    "tomss_linux_authentication_outside_australia.json",  # only used by 'tomss'
    "tomss_linux_authentication_outside_business_hours.json",  # only used by 'tomss'
    "tomss_linux_brute_force_attack.json",  # only used by 'tomss'
    "tomss_linux_changes_to_ipagroups.json",  # only used by 'tomss'
    "tomss_linux_changes_to_otp.json",  # only used by 'tomss'
    "tomss_linux_changes_to_promiscuous_mode.json",  # only used by 'tomss'
    "tomss_linux_changes_to_user_group_account.json",  # only used by 'tomss'
    "tomss_linux_multiple_user_accounts_locked.json",  # only used by 'tomss'
    "tomss_linux_sys_promiscuous_mode.json",  # only used by 'tomss'
    "tomss_linux_system_shut_reboot.json",  # only used by 'tomss'
    "tomss_linux_topcat_brute_force.json",  # only used by 'tomss'. Uses a single aggregation as it does not require tenant aggregation
    "tomss_linux_topcat_successful_login.json",  # only used by 'tomss'
    "tomss_linux_unconventional_login.json",  # only used by 'tomss'
    "tomss_linux_user_created_outside_business_hours.json",  # only used by 'tomss'
    "tomss_panos_vulnerability_threat.json",  # only used by 'tomss'
    "vodafonenz_aws_symbee_admin_successful_login.json",  # only used by 'vodafonenz'. Uses cron scheduling in elastic watcher
    "wknzta_ios_breakglass_account_failed_login_p2.json",  # only used by 'wknzta'
    "wknzta_ios_breakglass_account_failed_login_p3.json",  # only used by 'wknzta'
    "wknzta_ios_breakglass_account_successful_login.json",  # only used by 'wknzta'
    "workday_auth_policy_modified.json",  # only used by 'rbnz'
    "workday_fuzzing_insider_threat.json",  # only used by 'rbnz'
    "workday_high_volume_request.json",  # only used by 'rbnz'
    "workday_impossible_travel.json",  # only used by 'rbnz'
    "workday_multiple_users_login_single_ip.json",  # only used by 'rbnz'
    "workday_session_hijack.json",  # only used by 'rbnz'
    "workday_single_user_login_multiple_ips.json",  # only used by 'rbnz'
    "workday_subscription_modified.json",  # only used by 'rbnz'
    "workday_superuser_priv_assigned.json",  # only used by 'rbnz'
    "workday_susp_admin_activity.json",  # only used by 'rbnz'
    "workday_susp_user_modification.json",  # only used by 'rbnz'
    "workday_user_account_creation.json",  # only used by 'rbnz'
    "workday_user_agent_hacktool.json",  # only used by 'rbnz'
    "workday_user_agent_overflow.json",  # only used by 'rbnz'. Uses 'script' block. Will require manual conversion
    "workday_user_agent_sqli.json",  # only used by 'rbnz'
    "workday_user_agent_xss.json",  # only used by 'rbnz'
]


def map_device_name(device_name: str) -> str:
    updated_device_names = {
        "azure_active_directory": "microsoft_entra_id",
        "azure_ad_sign_on": "microsoft_entra_id_sign_on",
        "azure_firewall": "microsoft_azure_firewall",
        "azure_sentinel": "microsoft_sentinel",
        "checkpoint_opseclea_cef": "checkpoint_opseclea",
        "citrix_netscaler_cef": "citrix_netscaler",
        "cyberark_pas": "cyberark_pam",
        "darktrace_eis": "darktrace",
        "darktrace_eis_cef": "darktrace",
        "f5_bigip_cef": "f5_bigip",
        "forcepoint_cs": "forcepoint_web_gateway",
        "hparuba_clearpass_cef": "hparuba_clearpass",
        "juniper_json": "juniper",
        "kaspersky_eps_json": "kaspersky_eps",
        "kaspersky_eps_syslog": "kaspersky_eps",
        "linux_redhat": "linux",
        "microfocus_niqidm": "netiq_idm",
        "pulsesecure_gateway": "ivanti_connect_secure",
        "symantec_proxysg": "broadcom_symantec_web_gateway",
        "windows_o365": "microsoft_defender_for_cloud_apps",
    }
    new_name = updated_device_names.get(device_name, device_name)
    return new_name


def is_regex_pattern(pattern: str) -> bool:
    # https://stackoverflow.com/questions/22014886/check-if-string-contains-pattern-for-regular-expression
    """Returns False if the pattern is normal string"""

    try:
        pmatch = re.match(pattern, pattern)
    except Exception as e:
        raise ValueError("Unable to parse regexp pattern /{}/".format(pattern)) from e

    if pmatch is None:
        # string doesn't match itself
        return True
    if not pmatch.span() == (0, len(pattern)):
        # string does not match itself entirely
        return True
    return False


def sql_column_fix_name(column_name):
    pushin_p_list = [
        "destination_host",
        "container_image",
        "object_attribute",
        "service_request_parameters",
        "service_request_parameters_orig",
        "threat_threat_matches",
        "threat_threat_matches_url",
        "threat_enrichments_indicator",
        "service_request_parameters_file",
        "service_request_parameters_object",
        "target_object",
    ]
    new_col_name = re.sub("[-.]", "_", column_name)
    new_col_name = re.sub("[^a-zA-Z0-9_]", "", new_col_name)
    new_col_name = new_col_name.lower()
    if new_col_name == "event_original":
        return "logoriginal"
    elif new_col_name in pushin_p_list:
        return f"{new_col_name}_p"
    return new_col_name


def get_mapped_field_name(field_name, field_map, logger, native_fields):
    """
    Returns the mapped field name if it exists in the field_map,
    otherwise returns the original field name. Logs the action taken.

    :param field_name: The original field name to look up in the field_map.
    :param field_map: Dictionary containing mappings from source field names to target field names.
    :return: The target field name if found, otherwise the original field name.
    """
    original_field = field_name
    if field_name in field_map:
        mapped_fields = field_map[field_name]
        return mapped_fields
    else:
        if not (native_fields):
            logger.warning(
                f"WARNING: Field '{original_field}' not found in field mapping"
            )
        return [sql_column_fix_name(original_field)]


class WatcherParser:
    def __init__(
        self,
        schema_map_path: str = "elastic_watcher_converter/mapping_detections_schemas/hypercol",
        logger: logging.Logger = None,
        native_fields: bool = False,
    ):
        self.logger = logger
        self.unique_fields = set()
        self.raise_on_error = True
        self.native_fields = native_fields

        if native_fields and schema_map_path.split("/")[-1] == "hypercol":
            schema_map_path = f"{'/'.join(schema_map_path.split('/')[:-1])}/openmss"

        self.logger.info(f"Schema mapping directory: '{schema_map_path}'")

        self.schema_maps = self._build_schema_maps(schema_map_path)
        self.logger.info(f"Built schema maps: {self.schema_maps}")

    def _build_schema_maps(self, schema_path: str) -> dict:
        """Builds mapping for select and where clauses from CSVs in schema directories.

        Args:
            schema_path (str): The path to the directory containing schema subdirectories.

        Returns:
            dict: A dictionary with two keys 'select_field_map' and 'where_field_map' containing the mappings.
        """
        select_field_map = {}
        where_field_map = {}

        for item in os.listdir(schema_path):
            source_schema_path = os.path.join(schema_path, item)
            if "mapping_for_where_fields.csv" in source_schema_path:
                continue
            elif not os.path.isdir(source_schema_path):
                self.logger.info(
                    f"Skipping config file or unrelated directory {source_schema_path}."
                )
                continue
            directory_name = os.path.basename(source_schema_path)
            try:
                select_csv_path = os.path.join(
                    source_schema_path, "mapping_for_select_fields.csv"
                )
                if self.native_fields:
                    where_csv_path = os.path.join(
                        schema_path, "mapping_for_where_fields.csv"
                    )
                else:
                    where_csv_path = os.path.join(
                        source_schema_path, "mapping_for_where_fields.csv"
                    )

                if not os.path.exists(select_csv_path):
                    self.logger.error(
                        f"CSV files missing in {source_schema_path}. Skipping this directory."
                    )
                    raise

                select_df = pd.read_csv(select_csv_path)
                where_df = pd.read_csv(where_csv_path)

                select_df = select_df.drop(columns=["comment"], errors="ignore")

                select_field_map[directory_name] = [
                    sql_column_fix_name(field)
                    for field in select_df["select_fields"].tolist()
                ]
                for index, row in where_df.iterrows():
                    if type(row["new_field"]) == str:
                        row["new_field"] = row["new_field"].split("|")
                    else:
                        row["new_field"] = []
                where_field_map[directory_name] = dict(
                    zip(where_df["es_field"], where_df["new_field"])
                )

            except pd.errors.EmptyDataError as e:
                self.logger.error(
                    f"Empty Data {select_csv_path}: {str(e)}", exc_info=True
                )
                raise e
            except pd.errors.ParserError as e:
                self.logger.error(
                    f"CSV parsing failed in  {select_csv_path} or {where_csv_path}: {str(e)}",
                    exc_info=True,
                )
                raise e
            except Exception as e:
                self.logger.error(
                    f"Unhandled error when parsing CSVs: {str(e)}", exc_info=True
                )
                raise e

        return {
            "select_field_map": select_field_map,
            "where_field_map": where_field_map,
        }

    def _raise_or_print_on_not_implemented(self, msg: str, watcher_name: str):
        if self.raise_on_error:
            raise NotImplementedError(f"{msg} is not implemented")
        else:
            self.logger.warning(
                f"Skipping {watcher_name} due to: {msg} is not implemented"
            )

    def _return_watchers_directories(self, dir_path: str) -> List[str]:
        subdirs = [f.path for f in os.scandir(dir_path) if f.is_dir()]
        subdirs_with_jsons = [
            subdir for subdir in subdirs if glob.glob(subdir + "/*.json")
        ]
        for subdir in subdirs:
            if glob.glob(subdir + "/smd"):
                subdirs_with_jsons.append(subdir + "/smd")
            if glob.glob(subdir + "/aggs"):
                subdirs_with_jsons.append(subdir + "/aggs")

        if len(subdirs_with_jsons) == 0:
            subdirs_with_jsons = [dir_path]

        return subdirs_with_jsons

    def _flatten_json_query(
        self, json_data: Dict[str, Any], filepath: str
    ) -> Dict[str, Any]:
        schedule = (
            json_data.get("trigger", {}).get("schedule", {}).get("interval", None)
        )
        indices = (
            json_data.get("input", {})
            .get("search", {})
            .get("request", {})
            .get("indices", [])
        )

        bool_filter = (
            json_data.get("input", {})
            .get("search", {})
            .get("request", {})
            .get("body", {})
            .get("query", {})
            .get("bool", {})
            .get("filter", [])
        )

        unique_values = lambda lst: list(set(lst))

        bool_must = (
            json_data.get("input", {})
            .get("search", {})
            .get("request", {})
            .get("body", {})
            .get("query", {})
            .get("bool", {})
            .get("must", [])
        )
        must_field_names = list(self._extract_terms_fields(bool_must))
        unique_must_fields = unique_values(must_field_names)

        bool_must_not = (
            json_data.get("input", {})
            .get("search", {})
            .get("request", {})
            .get("body", {})
            .get("query", {})
            .get("bool", {})
            .get("must_not", [])
        )
        must_not_field_names = list(self._extract_terms_fields(bool_must_not))
        unique_must_not_fields = unique_values(must_not_field_names)

        bool_should = (
            json_data.get("input", {})
            .get("search", {})
            .get("request", {})
            .get("body", {})
            .get("query", {})
            .get("bool", {})
            .get("should", [])
        )
        should_field_names = list(self._extract_terms_fields(bool_should))
        unique_should_fields = unique_values(should_field_names)

        aggs = (
            json_data.get("input", {})
            .get("search", {})
            .get("request", {})
            .get("body", {})
            .get("aggs", {})
        )

        condition_script = (
            json_data.get("condition", {}).get("script", {}).get("source", None)
        )

        action_transform_script = (
            json_data.get("actions", {})
            .get("index_payload", {})
            .get("transform", {})
            .get("script", {})
            .get("source", None)
        )
        action_transform_params = (
            json_data.get("actions", {})
            .get("index_payload", {})
            .get("transform", {})
            .get("script", {})
            .get("params", None)
        )
        action_index = (
            json_data.get("actions", {})
            .get("index_payload", {})
            .get("index", {})
            .get("index", None)
        )

        metadata = json_data.get("metadata", {})
        filename = os.path.split(filepath)[1]

        es_body = (
            json_data.get("input", {})
            .get("search", {})
            .get("request", {})
            .get("body", {})
        )
        detection_type, agg_names, watcher_query = self._normalise_and_classify_es_body(
            es_body
        )

        es_trigger = json_data.get("trigger", {})
        schedule_duration = self._convert_es_schedule_to_string(es_trigger)

        data = {
            "filename": filename,
            "schedule": schedule,
            "indices": indices,
            "filter_range": bool_filter,
            "must_terms": unique_must_fields,
            "must_not_terms": unique_must_not_fields,
            "should_terms": unique_should_fields,
            "aggs": aggs,
            "condition_script": condition_script,
            "action_transform_script": action_transform_script,
            "action_transform_params": action_transform_params,
            "action_index": action_index,
            "es_body": es_body,
            "schedule_duration": schedule_duration,
            "detection_type": detection_type,
            **metadata,
        }
        return data

    def _process_file(self, filename: str) -> Dict[str, Any]:
        try:
            with open(filename, "r") as file:
                json_data = json.load(file)
                return self._flatten_json_query(json_data, filename)
        except Exception as e:
            self.logger.error(
                f"An error occurred while processing file {filename}: {repr(e)}"
            )
            raise

    def _process_directories(self, paths: List[str]) -> pd.DataFrame:
        data = []
        for path in paths:
            try:
                filenames = [
                    os.path.join(path, fn)
                    for fn in os.listdir(path)
                    if fn.endswith(".json") and fn not in warn_on_file
                ]
                data.extend([self._process_file(fn) for fn in filenames])
            except Exception as e:
                self.logger.error(
                    f"An error occurred while processing directory {path}: {e}"
                )
                continue
        if len(data) == 0:
            self.logger.error(f"There is no watchers in the paths provided[{paths}]")

        return pd.DataFrame(data)

    def _extract_terms_fields(self, data: Any) -> List[str]:
        if isinstance(data, list):
            for item in data:
                yield from self._extract_terms_fields(item)
        elif isinstance(data, dict):
            for key, value in data.items():
                if key in ["bool", "should", "must", "must_not"]:
                    yield from self._extract_terms_fields(value)
                elif key == "minimum_should_match":
                    return
                elif key == "exists":
                    yield value["field"]
                elif key == "query_string":
                    yield value["default_field"]
                elif key in [
                    "match",
                    "match_phrase",
                    "range",
                    "regexp",
                    "term",
                    "terms",
                    "wildcard",
                ]:
                    yield from self._extract_terms_fields(value)
                elif key == "script":
                    self.logger.error(
                        "A script block has been identified whilst extracting terms. This will be skipped."
                    )
                else:
                    yield key

    def _parse_action_transform_scripts(
        self, df: pd.DataFrame, column_name: str
    ) -> pd.DataFrame:
        # Define the pattern to match the lines within the script
        pattern = (
            r"v\.get\('_source'\)\.put\('([^']*)',\s*('([^']*)'|\d+|true|false)\);"
        )

        # New columns to be added based on the patterns found
        new_columns = {
            "threat.detection": [],
            "threat.framework": [],
            "threat.technique.id": [],
            "threat.technique.name": [],
            "threat.technique.reference": [],
            "threat.tactic.id": [],
            "threat.tactic.name": [],
            "threat.tactic.reference": [],
            "threat.is_alert": [],
            "threat.ratingtime_sla_applies": [],
            "threat.alert_type": [],
            "threat.detection_description": [],
            "threat.severity": [],
            "threat.triage_score": [],
        }

        # Iterate over each row in the DataFrame
        for index, script in df[column_name].items():
            if script is None or not isinstance(script, str):
                print(
                    f"Warning: Missing or invalid 'action_transform_script' at row {df.loc[index, 'filename']}"
                )
                # Append None or a default value for each new column for this row
                for key in new_columns.keys():
                    new_columns[key].append(None)
                continue

            # Find all matches in the current script
            matches = re.findall(pattern, script)

            # Initialize a temporary dictionary to hold found values for this row
            temp_dict = {key: None for key in new_columns.keys()}

            # Iterate through and remove the last element of the matches tuples
            for index, match in enumerate(matches):
                matches[index] = (match[0], match[1].strip("'"))

            # Process each match, updating temp_dict with the values found
            for match in matches:
                key, value = match
                if key in temp_dict:
                    temp_dict[key] = value

            # Append the found values (or None if not found) to the corresponding lists
            for key in new_columns.keys():
                new_columns[key].append(temp_dict[key])

        # Update the DataFrame with the new columns
        for key, values in new_columns.items():
            df[key] = values

        return df

    def _convert_es_schedule_to_string(self, es_schedule: Dict[str, Any]) -> str:
        """Convert Elasticsearch schedule block to a string value"""

        def loop_dictionary(dictionary):
            list_dictionary = {}
            for key, value in dictionary.items():
                if type(value) == list:
                    list_dictionary[key] = value
                elif type(value) == dict:
                    return loop_dictionary(value)
            return list_dictionary

        def convert_to_std_dict(non_std_list):
            std_dict = {"hour": [], "minute": []}
            for value in non_std_list:
                hour, minute = map(int, value.split(":"))
                if hour not in std_dict["hour"]:
                    std_dict["hour"].append(hour)
                if minute not in std_dict["minute"]:
                    std_dict["minute"].append(minute)
            return std_dict

        def convert_to_list(list_dictionary):
            if contains_multiple(list_dictionary, "at"):
                return convert_to_list(convert_to_std_dict(list_dictionary["at"]))
            elif contains_multiple(list_dictionary, "minute"):
                if len(list_dictionary["minute"]) == 2:
                    return [
                        str(list_difference(sorted(list_dictionary["minute"]))) + "m",
                        sorted(list_dictionary["minute"])[0] % 30,
                    ]
                else:
                    return [
                        str(list_difference(sorted(list_dictionary["minute"]))) + "m",
                        sorted(list_dictionary["minute"])[0] % 10,
                    ]
            elif contains_multiple(list_dictionary, "hour"):
                return [
                    str(list_difference(sorted(list_dictionary["hour"]))) + "h",
                    sorted(list_dictionary["minute"])[0],
                ]
            elif (
                len(list_dictionary["minute"]) == 1
                and len(list_dictionary["hour"]) == 1
            ):
                return ["24h", list_dictionary["minute"][0]]
            else:
                raise NotImplementedError(
                    f"Schedule {list_dictionary} cannot be handled"
                )

        list_dictionary = {}
        schedule_block = es_schedule.get("schedule", [])

        list_difference = lambda list_values: max(
            [list_values[i] - list_values[i - 1] for i in range(1, len(list_values))]
        )
        contains_multiple = (
            lambda dict_to_check, match: True
            if (match in dict_to_check and len(dict_to_check[match]) > 1)
            else False
        )
        if "interval" in schedule_block:
            return [schedule_block["interval"], 0]
        else:
            list_dictionary = loop_dictionary(schedule_block)
            return convert_to_list(list_dictionary)

    def _extract_device_and_customer_from_indices(
        self, es_indices: Dict[str, Any]
    ) -> Dict[str, List[str]]:
        """
        Converts Elasticsearch indices into a dictionary mapping device types to customers.

        Args:
        es_indices (Dict[str, Any]): A dictionary containing a list of indices.

        Returns:
        Dict[str, List[str]]: A dictionary where keys are device types and values are lists of customers.
        """
        indices_dictionary = {}

        for index in es_indices.get("indices", []):
            index_components = index.split("-")

            # Handle generic pattern '*-parsed*' directly
            if len(index_components) < 3 or index_components[0] == "*":
                return {"*": "*"}

            # Adjust index components if they exceed expected segments
            while len(index_components) > 3:
                if index_components[-1] == "*":
                    index_components = index_components[:-2] + [
                        index_components[-2] + "-" + index_components[-1]
                    ]
                else:
                    if (
                        index_components[0] == "rbnz"
                        and index_components[1] == "payments"
                    ):
                        index_components = [
                            index_components[0] + "-" + index_components[1]
                        ] + index_components[2:]
                    else:
                        index_components = (
                            [index_components[0]]
                            + [index_components[1] + "-" + index_components[2]]
                            + index_components[3:]
                        )

            # Populate the dictionary with adjusted index components
            device_type, customer = (
                map_device_name(index_components[1]),
                index_components[0],
            )

            if device_type not in indices_dictionary:
                indices_dictionary[device_type] = [customer]
            else:
                if customer not in indices_dictionary[device_type]:
                    indices_dictionary[device_type].append(customer)

        return indices_dictionary

    def _convert_es_query_to_sql_where(
        self,
        es_query: Dict[str, Any],
        file_name: str = None,
        field_mappings: Dict[str, str] = None,
        logger: logging.Logger = None,
    ) -> str:
        """Convert Elasticsearch query DSL to SQL WHERE clause."""

        def handle_exists(condition) -> str:
            exists_clause = ""
            exists_clause_sublist = []
            for field, value in condition.items():
                if field == "field":
                    # value = sql_column_fix_name(value)
                    # self.unique_fields.add(value)
                    mapped_fields = get_mapped_field_name(
                        value, field_mappings[0], logger, self.native_fields
                    )

                    for mapped_field in mapped_fields:
                        # TODO
                        # if (re.match(r"^\d+%$", sql_like)):
                        # If find type of mapped field is string do below else CAST it
                        exists_clause_sublist.append(
                            f"({mapped_field} IS NOT NULL AND {mapped_field} != '')"
                        )
                else:
                    raise NotImplementedError(
                        f"For exists clause, cannot handle parameter {field}"
                    )
            exists_clause = " OR ".join(exists_clause_sublist)
            return exists_clause

        def handle_match(condition) -> str:
            match_clause = ""
            match_clause_sublist = []
            for field, value in condition.items():
                # field = sql_column_fix_name(field)
                # self.unique_fields.add(field)
                mapped_fields = get_mapped_field_name(
                    field, field_mappings[0], logger, self.native_fields
                )

                if type(value) == dict:
                    for sub_field, sub_value in value.items():
                        if sub_field == "query":
                            for mapped_field in mapped_fields:
                                match_clause_sublist.append(
                                    f"{mapped_field} == '{sub_value}'"
                                )
                        else:
                            raise NotImplementedError(
                                f"For match clause, cannot handle parameter {sub_field}"
                            )
                else:
                    for mapped_field in mapped_fields:
                        match_clause_sublist.append(f"{mapped_field} == '{value}'")
            match_clause = " OR ".join(match_clause_sublist)
            return match_clause

        def handle_match_phrase(condition) -> str:
            match_phrase_clause = ""
            match_phrase_clause_sublist = []
            for field, value in condition.items():
                if type(value) == dict:
                    for sub_field, sub_value in value.items():
                        if sub_field == "query":
                            # field = sql_column_fix_name(field)
                            # self.unique_fields.add(field)
                            mapped_fields = get_mapped_field_name(
                                field, field_mappings[0], logger, self.native_fields
                            )

                            for mapped_field in mapped_fields:
                                match_phrase_clause_sublist.append(
                                    f"{mapped_field} == '{sub_value}'"
                                )
                        else:
                            raise NotImplementedError(
                                f"For match_phrase clause, cannot handle parameter {sub_field}"
                            )
                else:
                    # field = sql_column_fix_name(field)
                    # self.unique_fields.add(field)
                    mapped_fields = get_mapped_field_name(
                        field, field_mappings[0], logger, self.native_fields
                    )

                    for mapped_field in mapped_fields:
                        match_phrase_clause_sublist.append(
                            f"{mapped_field} == '{value}'"
                        )
            match_phrase_clause = " OR ".join(match_phrase_clause_sublist)
            return match_phrase_clause

        def handle_query_string(condition) -> str:
            query_string_clause = ""
            query_string_clauses_sublist = []
            regex_val = ""
            contains_or_and = False
            for field, value in condition.items():
                if field == "default_field":
                    # query_field = sql_column_fix_name(value)
                    # self.unique_fields.add(mapped_field)
                    mapped_fields = get_mapped_field_name(
                        value, field_mappings[0], logger, self.native_fields
                    )
                elif field == "query":
                    index = 0
                    while index < len(value):
                        if (
                            index > 0
                            and value[index] == "*"
                            and value[index - 1] != "."
                        ) or (index == 0 and value[index] == "*"):
                            regex_val += ".*"
                            index += 1
                        else:
                            regex_val += value[index]
                            index += 1
                    regex_val = regex_val.replace("\\", "\\\\")
                    sql_like = regex_val.replace("*", "%")
                    if " OR " in value or "|" in value:
                        regex_val = regex_val.replace(" OR ", "|")
                        contains_or_and = True
                    # This only exists in one detection (microsoft_cloud_security_medium_azure_atp)
                    if " AND " in value:
                        new_regex_val = ""
                        regex_val_split = regex_val.split(" AND ")
                        for split_value in regex_val_split:
                            new_regex_val = (
                                new_regex_val + "(?=.*" + split_value + ").*"
                            )
                        regex_val = new_regex_val[:-2]
                        contains_or_and = True
                else:
                    raise NotImplementedError(
                        f"For query_string clause, cannot handle parameter {field}"
                    )
            if contains_or_and or is_regex_pattern(regex_val):
                for mapped_field in mapped_fields:
                    query_string_clauses_sublist.append(
                        f"match({mapped_field}, '{regex_val}')"
                    )
            else:
                sql_like = sql_like.replace("'", "''")
                for mapped_field in mapped_fields:
                    query_string_clauses_sublist.append(
                        f"{mapped_field} LIKE '{sql_like}'"
                    )
            query_string_clause = " OR ".join(query_string_clauses_sublist)
            return query_string_clause

        def handle_range(condition) -> str:
            range_clauses = ""
            range_clauses_sublist = []
            for field, ranges in condition.items():
                # field = sql_column_fix_name(field)
                # self.unique_fields.add(field)

                if field == "@timestamp":
                    continue
                    # Handle Timestamp in Alert Framework
                    # for key in ranges:
                    #     # this is to treat any detections that may deviate from the general timestamp format of gte and lt (i.e gt and lte)
                    #     # and also to combat unordered nature of dict
                    #     if (key in ["gt", "gte"]):
                    #         ranges[key] = "{{from_timestamp}}"
                    #     elif (key in ["lt", "lte"]):
                    #         ranges[key] = "{{to_timestamp}}"
                    #     else:
                    #         raise NotImplementedError(f"For range clause, in timestamp, cannot handle key {key}")

                mapped_fields = get_mapped_field_name(
                    field, field_mappings[0], logger, self.native_fields
                )

                for mapped_field in mapped_fields:
                    range_clauses_sublist.append(
                        [
                            f"""{mapped_field} {
                                ">"
                                if op == "gt"
                                else ">="
                                if op == "gte"
                                else "<"
                                if op == "lt"
                                else "<="
                                if op == "lte"
                                else ""
                            } {val}"""
                            for op, val in ranges.items()
                        ]
                    )
            range_clauses = (
                " AND ".join(range_clauses_sublist[0])
                if (len(range_clauses_sublist) > 0)
                else ""
            )
            return range_clauses

        def construct_regexp_clause(
            field: str, value: str, is_case_insensitive: bool
        ) -> str:
            """Constructs a regex clause based on the provided parameters."""
            regexp_clauses_sublist = []
            # field = sql_column_fix_name(field)
            # self.unique_fields.add(field)
            mapped_fields = get_mapped_field_name(
                field, field_mappings[0], logger, self.native_fields
            )

            value_index = 0
            is_not_escaped = lambda index: value[index - 1] != "\\"
            characters_to_escape = ["'"]

            value = value.replace("\\", "\\\\")

            for character in value:
                if character in characters_to_escape and (
                    value_index + 1 < len(value)
                    and value[value_index : value_index + 2] != ".*"
                ):
                    if value_index - 1 >= 0 and is_not_escaped(value_index):
                        value = value[:value_index] + "\\" + value[value_index:]
                        value_index += 1
                elif character in characters_to_escape:
                    value = value[:value_index] + "\\" + character
                    value_index += 1
                value_index += 1
            sql_like = value.replace(".*", "%")
            for mapped_field in mapped_fields:
                if is_case_insensitive:
                    regexp_clauses_sublist.append(
                        f"match({mapped_field}, '(?i:({value}))')"
                    )
                elif is_regex_pattern(value) or "|" in value:
                    regexp_clauses_sublist.append(f"match({mapped_field}, '{value}')")
                elif ".*" not in value:
                    regexp_clauses_sublist.append(f"{mapped_field} == '{value}'")
                else:
                    # Convert to SQL LIKE syntax if not a regex pattern
                    sql_like = sql_like.replace("'", "''")
                    regexp_clauses_sublist.append(f"{mapped_field} LIKE '{sql_like}'")
            return " OR ".join(regexp_clauses_sublist)

        def handle_regex(condition) -> str:
            regexp_clauses_list = []
            for field, values in condition.items():
                # ensure values is a dict with 'value' key
                if isinstance(values, str):
                    values = {"value": values}
                regex_val = values.get("value", "")
                is_case_insensitive = values.get("case_insensitive", False)
                if not regex_val:
                    raise ValueError("Regex value is missing")
                clause = construct_regexp_clause(field, regex_val, is_case_insensitive)
                regexp_clauses_list.append(clause)
            return " AND ".join(regexp_clauses_list)

        def handle_term(condition) -> str:
            term_clause = ""
            term_clause_sublist = []
            for field, values in condition.items():
                # field = sql_column_fix_name(field)
                # self.unique_fields.add(field)
                mapped_fields = get_mapped_field_name(
                    field, field_mappings[0], logger, self.native_fields
                )
                if isinstance(values, str):
                    values = {"value": values}
                for sub_field, value in values.items():
                    if sub_field == "value":
                        sql_value = value
                    else:
                        raise NotImplementedError(
                            f"For term clause, cannot handle parameter {sub_field}"
                        )
                for mapped_field in mapped_fields:
                    term_clause_sublist.append(f"{mapped_field} == '{sql_value}'")
                term_clause = " OR ".join(term_clause_sublist)
            return term_clause

        def handle_terms(condition) -> str:
            terms_clauses_list = []
            terms_clauses_sublist = []
            for field, terms in condition.items():
                # field = sql_column_fix_name(field)
                # self.unique_fields.add(field)
                mapped_fields = get_mapped_field_name(
                    field, field_mappings[0], logger, self.native_fields
                )

                string_of_terms = "("
                if type(terms) == str:
                    # handle string terms, e.g. aws_cloudtrail_admin_privileges.json
                    terms = [terms]
                if type(terms) == list:
                    for term in terms:
                        if type(term) == int:
                            term = str(term)
                        term = term.replace("'", "\\'")
                        string_of_terms += f"'{term}', "
                else:
                    raise NotImplementedError(
                        f"For terms clause, cannot handle parameter {field}"
                    )
                string_of_terms = string_of_terms[:-2] + ")"

                for mapped_field in mapped_fields:
                    terms_clauses_sublist.append(f"{mapped_field} IN {string_of_terms}")
                terms_clauses_list.append(" OR ".join(terms_clauses_sublist))
            terms_clauses = " AND ".join(terms_clauses_list)
            return terms_clauses

        def handle_wildcard(condition) -> str:
            wildcard_clause = ""
            wildcard_clause_sublist = []
            for field, values in condition.items():
                # self.unique_fields.add(field)
                mapped_fields = get_mapped_field_name(
                    field, field_mappings[0], logger, self.native_fields
                )

                for sub_field, value in values.items():
                    if sub_field == "value":
                        sql_value = value.replace("*", "%").replace("?", "_")
                    elif sub_field == "case_insensitive":
                        pass
                        # do nothing, handle it later
                    else:
                        raise NotImplementedError(
                            f"For wildcard clause, cannot handle parameter {sub_field}"
                        )

                is_case_insensitive = values.get("case_insensitive", False)
                for mapped_field in mapped_fields:
                    if is_case_insensitive:
                        wildcard_clause_sublist.append(
                            f"{mapped_field} ILIKE '{sql_value}'"
                        )
                    else:
                        wildcard_clause_sublist.append(
                            f"{mapped_field} LIKE '{sql_value}'"
                        )

            wildcard_clause = " OR ".join(wildcard_clause_sublist)
            return wildcard_clause

        def handle_cluster(cluster_type, cluster):
            if "exists" == cluster_type:
                exists_clauses = handle_exists(cluster)
                return exists_clauses
            elif "match" == cluster_type:
                match_clauses = handle_match(cluster)
                return match_clauses
            elif "match_phrase" == cluster_type:
                match_phrase_clauses = handle_match_phrase(cluster)
                return match_phrase_clauses
            elif "query_string" in cluster_type:
                query_string_clauses = handle_query_string(cluster)
                return query_string_clauses
            elif "range" == cluster_type:
                range_clauses = handle_range(cluster)
                return range_clauses
            elif "regexp" == cluster_type:
                regexp_clauses = handle_regex(cluster)
                return regexp_clauses
            elif "term" == cluster_type:
                term_clauses = handle_term(cluster)
                return term_clauses
            elif "terms" == cluster_type:
                terms_clauses = handle_terms(cluster)
                return terms_clauses
            elif "wildcard" == cluster_type:
                wildcard_clauses = handle_wildcard(cluster)
                return wildcard_clauses
            elif "script" == cluster_type:
                self._raise_or_print_on_not_implemented(
                    "handle_condition - script", watcher_name=file_name
                )
            else:
                raise NotImplementedError(
                    f"Condition type {cluster_type} has not been implemented"
                )

        def handle_should_cluster(cluster_type, cluster, clause_list):
            clause_list.extend([cluster_type, "("])
            for clause in cluster:
                for clause_type, conditions in clause.items():
                    if clause_type == "bool":
                        if count_bool_conditions(clause_type, conditions) > 1:
                            clause_list.extend(["bool", "("])
                            loop_items(conditions, 0, clause_list)
                            clause_list.append(")")
                        else:
                            loop_items(conditions, 0, clause_list)
                    else:
                        if type(conditions) == dict:
                            clause_list.append(handle_cluster(clause_type, conditions))
            clause_list.append(")")

            return clause_list

        def count_bool_conditions(clause_type, conditions):
            count = 0
            if clause_type == "bool":
                for condition in conditions:
                    if condition != "minimum_should_match":
                        count += 1
            return count

        def loop_items(dictionary, start_index, clause_list):
            for cluster_type, cluster in dictionary.items():
                if cluster_type == "bool":
                    if count_bool_conditions(cluster_type, cluster) > 1:
                        clause_list.extend(["bool", "("])
                        clause_list = loop_items(cluster, start_index, clause_list)
                        clause_list.append(")")
                    else:
                        clause_list = loop_items(cluster, start_index, clause_list)
                else:
                    if cluster_type == "minimum_should_match":
                        clause_list.append(cluster)
                        continue
                    elif cluster_type in ["filter", "must", "must_not", "should"]:
                        if type(cluster) == dict:
                            cluster = [cluster]
                        if cluster_type == "should":
                            clause_list = handle_should_cluster(
                                cluster_type, cluster, clause_list
                            )
                            continue

                        clause_list.append(cluster_type)
                    if type(cluster) == dict:
                        clause_list.append(handle_cluster(cluster_type, cluster))
                    elif type(cluster) == list:
                        inner_index = start_index
                        start_index = start_index + 1
                        for clause in cluster:
                            clause_list.append("(")
                            clause_list = loop_items(clause, start_index, clause_list)
                            clause_list.append(")")
                        start_index = inner_index
                    else:
                        start_index = start_index - 1
            return clause_list

        def extract_should_conditions(sql_clauses, start_index):
            sub_condition_count = 0
            for clause in sql_clauses[start_index:]:
                if sub_condition_count == 0 and (
                    type(clause) == int or (type(clause) == str and clause.isdigit())
                ):
                    return int(clause)
                elif clause == "(":
                    sub_condition_count += 1
                elif clause == ")":
                    sub_condition_count -= 1
            return

        def count_should_conditions(sql_clauses, start_index):
            index = start_index
            nested_conditions = []
            sub_condition_count = 0
            while sql_clauses[index] != ")" or sub_condition_count != 0:
                if sql_clauses[index] == "(":
                    sub_condition_count += 1
                elif sql_clauses[index] == ")":
                    sub_condition_count -= 1
                elif sub_condition_count == 0 and not (
                    type(sql_clauses[index]) == int
                    or (
                        type(sql_clauses[index]) == str and sql_clauses[index].isdigit()
                    )
                ):
                    nested_conditions.append(sql_clauses[index])
                index += 1
            return len(nested_conditions)

        def find_delimiter(stack):
            if len(stack) < 1 or stack[-1] == "must":
                return "AND"
            elif stack[-1] == "must_not":
                return "OR"
            elif "should" in stack[-1]:
                if stack[-1][1] == stack[-1][2]:
                    return "AND"
                elif stack[-1][1] == 1 and stack[-1][1] < stack[-1][2]:
                    return "OR"
                elif stack[-1][1] == None:
                    return "OR"
            elif stack[-1] == "bool":
                return "AND"

        def format_sql_clauses_list(sql_clauses):
            final_string = ""
            stack = []
            delimiter = ""
            for index, clause in enumerate(sql_clauses):
                delimiter = find_delimiter(stack)
                if type(clause) == int or (type(clause) == str and clause.isdigit()):
                    continue
                elif clause in ["filter", "must", "must_not", "should", "bool"]:
                    if clause == "filter":
                        continue
                    elif clause == "must_not":
                        final_string += "NOT "
                elif clause in ["(", ")"]:
                    if clause == "(":
                        if sql_clauses[index - 1] in [
                            "filter",
                            "must",
                            "must_not",
                            "should",
                            "bool",
                        ]:
                            if sql_clauses[index - 1] == "should":
                                stack.append(
                                    [
                                        "should",
                                        extract_should_conditions(
                                            sql_clauses, index - 1
                                        ),
                                        count_should_conditions(sql_clauses, index + 1),
                                    ]
                                )
                            else:
                                stack.append(sql_clauses[index - 1])
                        elif sql_clauses[index - 1] == ")":
                            if final_string.strip().split(" ")[-1] not in ["OR", "AND"]:
                                final_string += f" {delimiter} "
                            continue
                        final_string += clause
                    elif clause == ")":
                        if index == len(sql_clauses) - 1:
                            stack.pop()
                        elif sql_clauses[index + 1] != "(":
                            stack.pop()
                        elif sql_clauses[index + 1] == "(":
                            continue
                        delimiter = find_delimiter(stack)
                        if (
                            index < len(sql_clauses) - 1
                            and sql_clauses[index + 1] != ")"
                            and not (
                                type(sql_clauses[index + 1]) == int
                                or (
                                    type(sql_clauses[index + 1]) == str
                                    and sql_clauses[index + 1].isdigit()
                                )
                            )
                        ):
                            final_string += f"{clause} {delimiter} "
                        elif (
                            index < len(sql_clauses) - 1
                            and sql_clauses[index + 1] == ")"
                        ):
                            final_string += f"{clause}"
                        elif index < len(sql_clauses) - 2 and sql_clauses[
                            index + 2
                        ] in ["must", "should", "must_not"]:
                            final_string += f"{clause} {delimiter} "
                        else:
                            final_string += f"{clause}"
                elif sql_clauses[index + 1] != ")" and not (
                    type(sql_clauses[index + 1]) == int
                    or (
                        type(sql_clauses[index + 1]) == str
                        and sql_clauses[index + 1].isdigit()
                    )
                ):
                    if len(clause) == 0:
                        continue
                    else:
                        final_string += f"{clause} {delimiter} "
                else:
                    final_string += f"{clause}"
            return final_string

        whitelist_data = self._remove_tenant_whitelisting(es_query)
        es_query = whitelist_data["q"]
        es_query_query = es_query.pop("query")
        try:
            bool_section = es_query_query.pop("bool")
        except KeyError:
            # we don't care if it can't find bool.
            bool_section = {}

        # remove ignored clauses
        if "size" in es_query.keys():
            es_query.pop("size")
        if "_source" in es_query.keys():
            es_query.pop("_source")
        # following occurs in ML detections only, we're not dealing with those.
        # if "sort" in es_query.keys():
        #     es_query.pop("sort")
        if "aggs" in es_query.keys():
            es_query.pop("aggs")  # TODO - need to remove this

        # check if there are any unhandled clauses left
        if len(es_query) > 0:
            raise NotImplementedError(
                f"Remaining keys on body: {[k for k in es_query.keys()]}"
            )
        if len(es_query_query) > 0:
            raise NotImplementedError(
                f"Remaining keys on query: {[k for k in es_query_query.keys()]}"
            )

        sql_clauses = loop_items(bool_section, 0, [])
        sql_clause_str = format_sql_clauses_list(sql_clauses)
        sql_clause_str = sql_clause_str.replace("() AND ", "")

        # get whitelist
        customer_queries = whitelist_data["customers"]

        # process whitelist
        customer_output = {}
        for customer, level_dict in customer_queries.items():
            customer_output[customer] = {}
            for level_name, levels in level_dict.items():
                for level in levels:
                    if level == {}:
                        continue
                    customer_clause_list = loop_items(level.pop("bool"), 0, [])
                    customer_output[customer][level_name] = format_sql_clauses_list(
                        customer_clause_list
                    )
            if customer_output[customer] == {}:
                del customer_output[customer]

        return {
            "where": sql_clause_str,
            "customers": customer_output,
            "split_logic_whitelist": whitelist_data["split_logic_whitelist"],
            # TODO do something with the split logic flag
        }

    def _check_for_hit(self, a, count):
        """
        Recursively checks for the aggregation called "hit"

        Parameters:
            a: obj
                An aggregation query
            count: list
                A list containing the aggregation names that lead to the current aggregation

        Returns:
            list. A list containing the aggregation names that lead to the current aggregation.
            The final list returned is the list of aggregation names that is traversed to the "hit" aggregation
        """
        k = a.keys()
        if "hit" in k:
            # if hit is an aggregation name, then we're done
            return count
        for key in k:
            # iterate through the aggregations and descend into them
            agg = a[key]
            if type(agg) == type({}) and "aggs" in agg.keys():
                return self._check_for_hit(agg["aggs"], count + [key])

        return count

    def _normalise_and_classify_es_body(
        self, es_query: Dict[str, Any]
    ) -> (str, list, dict):
        """
        Determine the detection type and normalise the aggregation names

        Parameters:
            None

        Returns:
            dict. The extracted search query with normalised aggregation names

        Raises:
            IndexError when there is no handling of the aggregation type
        """
        # List of bucket names to normalise to.
        # the first element is the name for the "tenant" bucket
        # if None, it uses the name that's already there
        normalise_aggs_to = [None, "bucket_2", "bucket_3", None]

        agg_names = {}
        # Find out if there are any aggs
        watcher_query = deepcopy(es_query)
        if "aggs" not in watcher_query.keys():
            detection_type = "smd"
        else:
            # if there are aggs, determine how deep they are

            # create a copy
            aggs = deepcopy(watcher_query)
            agg = aggs["aggs"]

            # recursively determine the path to the hits
            hit_path = self._check_for_hit(agg, [])
            agg_depth = len(hit_path)

            # normalise names
            for i, path in enumerate(hit_path):
                if normalise_aggs_to[i] is None:
                    agg_names[path] = path
                else:
                    # rename
                    agg[normalise_aggs_to[i]] = agg.pop(path)
                    agg_names[path] = normalise_aggs_to[i]
                agg = agg[agg_names[path]]["aggs"]

            watcher_query = aggs
            # detection names - index is the number of aggregations
            names = [
                None,  # 0 aggs not expected
                None,  # 1 agg not expected
                "two aggs",
                "three aggs",
                "four aggs",
            ]

            try:
                detection_type = names[agg_depth]
            except IndexError:
                raise NotImplementedError(f"Not handled for agg_depth of {agg_depth}")

            if detection_type is None:
                raise NotImplementedError(f"Not handled for agg_depth of {agg_depth}")

        return detection_type, agg_names, watcher_query

    def _remove_tenant_whitelisting(self, query: dict) -> dict:
        """
        Removes any tenant whitelisting logic from the given query

        Parameters:
            query: obj
                The query to remove whitelist from

        Returns:
            obj.
            {
                "q": obj, The query parameter after removing any tenant whitelisting
                "customers": {
                    customer_name: str, whitelist
                    ...
                }
            }

        Raises:
            ValueError when whitelist removal fails
        """

        def check_bool_subtree(subtree, l, whitelist, found_whitelist, split_logic):
            subtree_copy = deepcopy(subtree)
            for key, subquery in subtree.items():
                if "tenant.name" in json.dumps(subquery):
                    if key == "should":
                        tlq = self._remove_tenant_in_should(subtree, level=l)
                        subtree_copy = tlq["logic"]
                        whitelist = tlq["whitelists"]
                        found_whitelist = tlq["found_tenant_whitelist"]

                        return (
                            subtree_copy,
                            whitelist,
                            found_whitelist,
                            tlq["split_logic_whitelist"],
                        )

                    for ind, subsubq in enumerate(subquery):
                        if "tenant.name" in json.dumps(subsubq):
                            if list(subsubq.keys())[0] == "bool":
                                (
                                    logic,
                                    whitelist,
                                    found_whitelist,
                                    split_logic_whitelist,
                                ) = check_bool_subtree(
                                    subsubq["bool"],
                                    l + 1,
                                    whitelist,
                                    found_whitelist,
                                    split_logic,
                                )
                                subtree_copy[key][ind]["bool"] = logic

                                return (
                                    subtree_copy,
                                    whitelist,
                                    found_whitelist,
                                    split_logic_whitelist,
                                )
                            else:
                                # direct removal - see STAGING-sunray_win_cve-2021_44228_2.json (modified)
                                for phrase_meta, logic_phrases in subtree.items():
                                    # should only run once?
                                    subtree_copy = deepcopy(subtree)
                                    for phrase in logic_phrases:
                                        if "terms" in phrase.keys():
                                            if "tenant.name" in phrase["terms"].keys():
                                                customers = phrase["terms"][
                                                    "tenant.name"
                                                ]
                                                subtree_copy[phrase_meta].remove(phrase)

                                found_tenant_whitelist = True
                                tree_return = {"bool": subtree_copy}
                                for customer in customers:
                                    wl = self._tenant_whitelist_dict_add_item(
                                        whitelist, l, customer, tree_return
                                    )

                                return (
                                    tree_return,
                                    wl,
                                    found_tenant_whitelist,
                                    split_logic,
                                )
                else:
                    continue

            return subtree, whitelist, found_whitelist, split_logic

        # Create a deep copy (see python shallow copy behaviour)
        q = deepcopy(query)

        # Pre-allocate customer data
        customer_data = {}

        # Determine whether or not there's a whitlist present
        query_string = json.dumps(q["query"])
        has_tenant_whitelist = False
        found_tenant_whitelist = False
        if "tenant.name" in query_string:
            has_tenant_whitelist = True
        else:
            # if no bool on tenant.name, then no whitelist and return the original query
            return {
                "q": q,
                "customers": {},
                "split_logic_whitelist": False,
            }

        # Do whitelist removal on must_not
        _, customer_data, found_tenant_whitelist, split_logic_whitelist = (
            {},
            {},
            False,
            False,
        )
        top_level_query = q["query"]["bool"]
        if "must_not" in top_level_query.keys():
            # This is currently just for netscaler_login_failed
            must_not_copy = deepcopy(top_level_query["must_not"])
            for item in must_not_copy:
                if "terms" in item.keys():
                    if "tenant.name" in item["terms"].keys():
                        found_tenant_whitelist = True
                        customer_data["top_level_must_not"].append(
                            top_level_query.pop("must_not")
                        )
                elif "bool" in item.keys() and "tenant.name" in json.dumps(item):
                    # going to assume single level nesting

                    # assume it must be a single must
                    subtree = deepcopy(item["bool"])
                    musts = subtree.pop("must")

                    if not subtree == {}:
                        raise NotImplementedError(
                            "Cannot handle complex top level must nots"
                        )

                    # walk through musts to determine applicable customers
                    for m in musts:
                        if "terms" in m.keys() and "tenant.name" in m["terms"].keys():
                            customers = m["terms"]["tenant.name"]
                            musts.remove(m)

                    # add whitelist data
                    wl = {"bool": {"must_not": [{"bool": {"must": musts}}]}}
                    for customer in customers:
                        customer_data = self._tenant_whitelist_dict_add_item(
                            customer_data, 0, customer, wl
                        )

                    top_level_query["must_not"].remove(item)
                    found_tenant_whitelist = True

        # Do the whitelist removal on should
        level = 0

        (
            top_level_query,
            customer_data,
            found_tenant_whitelist,
            split_logic_whitelist,
        ) = check_bool_subtree(
            top_level_query,
            level,
            customer_data,
            found_tenant_whitelist,
            split_logic_whitelist,
        )
        q["query"]["bool"] = top_level_query

        # do another check for remaining whitelisting
        if not has_tenant_whitelist == found_tenant_whitelist:
            raise ValueError(
                "Tenant whitelist removal was not successful - tenant.name query not found."
            )
        if "tenant.name" in json.dumps(top_level_query):
            raise ValueError(
                "Tenant whitelist removal was not successful - tenant.name still present."
            )

        return {
            "q": q,
            "customers": customer_data,
            "split_logic_whitelist": split_logic_whitelist,
        }

    def _tenant_whitelist_dict_add_item(
        self, d: dict, l: int, c: str, item: dict
    ) -> dict:
        # d = dict to add to, l = level, c = customer

        # check for existing customer dict
        if c not in d.keys():
            d[c] = {}

        # create level if it doesn't exist
        if l not in d[c].keys():
            d[c][l] = []

        d[c][l].append(item)

        return d

    def _remove_tenant_in_should(
        self, input: dict, level: int = 0, removed_items={}
    ) -> dict:
        """
        Recursive function to look for terms queries on tenant.name inside a should query

        Parameters:
            input: dict
                Object containing the top level query

        Returns:
            dict. Contains the input dict without any tenant whitelisting

        Raises:
            NotImplementedError on unexpected code branches

        Warns:
            when minimum_should_match is changed

        ---

        Remove tenant whitelist logic:
            Iterate through the elements of the should, doing the following
                1.  Check whether it's a bool query, and if it's nested. If so, recursively call this function
                    If the output is different, remove it (in some cases mark it for removal later)
                2.  Search through the rest of the searches for term/terms queries on tenant.name
                    Remove this item
                    If it's a must_not, check if there are additional queries at the same level. If there are, there is additional logic and call split_logic_should, returning that result
            If whitelisting is found, remove any additionally marked items, then clean up should and minimum_should_match if it is empty
            If minimum_should_match is greater than 1, decrement it and warn
        """

        found_tenant_whitelist = True

        # Make some copies
        input_copy = deepcopy(input)
        should = input["should"]
        should_copy = deepcopy(should)

        # allocate some defaults
        found_tenant = False
        pop_items = []
        removeds = []

        #  Go through should
        for i1, item in enumerate(should_copy):
            if "bool" in item.keys():
                item_bool = item["bool"]
                for key, val in item_bool.items():
                    if "should" == key:
                        # if it's nested, recursively call this function
                        nested_should = self._remove_tenant_in_should(
                            deepcopy(item_bool), level + 1, deepcopy(removed_items)
                        )

                        nested = nested_should["logic"]
                        for customer, levels in nested_should["whitelists"].items():
                            for lev, level_items in levels.items():
                                for item in level_items:
                                    removed_items = (
                                        self._tenant_whitelist_dict_add_item(
                                            removed_items, lev, customer, item
                                        )
                                    )

                        if item_bool == nested:
                            pass
                        elif len(nested) > 0:
                            input["should"][i1]["bool"] = nested
                            continue
                        else:
                            pop_items.append(item)
                            continue
                    # Need it to be a list for enumerate
                    if not type(val) == type([]):
                        val = [val]

                    # go through the bits in clause
                    for t in val:
                        #  terms must be dict
                        if not type(t) == type({}):
                            continue

                        if "term" in t.keys():
                            # convert term to terms query
                            te = {}
                            for k, v in t["term"].items():
                                te[k] = [v["value"]]
                            t = {"terms": te}

                        if "terms" in t.keys():
                            if "tenant.name" in t["terms"].keys():
                                # remove it item
                                found_tenant = True

                                # check for split logic condition
                                if key == "must_not" and len(item_bool.keys()) > 1:
                                    should.remove(item)
                                    found_tenant_whitelist = True

                                    sls = self._split_logic_should(input_copy)

                                    for wl in sls["whitelists"]:
                                        wl_copy = deepcopy(wl)
                                        for bool_type, val in wl["bool"].items():
                                            for phrase in val:
                                                for (
                                                    phrase_type,
                                                    phrase_contents,
                                                ) in phrase.items():
                                                    if (
                                                        bool_type == "must"
                                                        and phrase_type == "terms"
                                                        and "tenant.name"
                                                        in phrase_contents.keys()
                                                    ):
                                                        customers = phrase_contents[
                                                            "tenant.name"
                                                        ]

                                                        wl_copy["bool"][
                                                            bool_type
                                                        ].remove(phrase)

                                        for customer in customers:
                                            removed_items = (
                                                self._tenant_whitelist_dict_add_item(
                                                    removed_items,
                                                    level,
                                                    customer,
                                                    wl_copy,
                                                )
                                            )

                                    sls["whitelists"] = removed_items
                                    sls["found_tenant_whitelist"] = (
                                        found_tenant_whitelist
                                    )
                                    sls["split_logic_whitelist"] = True

                                    return sls
                                elif key == "must_not":
                                    # removing single customer must_nots
                                    should.remove(item)
                                else:
                                    customers = t["terms"]["tenant.name"]
                                    subitem = deepcopy(item)
                                    del subitem["bool"][key]
                                    for customer in customers:
                                        removed_items = (
                                            self._tenant_whitelist_dict_add_item(
                                                removed_items, level, customer, subitem
                                            )
                                        )

                                    should.remove(item)

                pass
            elif "terms" in item.keys():
                if "tenant.name" in item["terms"].keys():
                    raise NotImplementedError("Not checking direct terms queries")
            else:
                # it's not a tenant.name
                if "tenant.name" in json.dumps(item):
                    raise NotImplementedError("This shouldn't ever happen")
                continue

        # Remove any additional marked items
        for item in pop_items:
            if level in removed_items.keys():
                removed_items[level].append(item)
            else:
                removed_items[level] = [item]
            input["should"].remove(item)
            raise NotImplementedError()

        if found_tenant:
            if len(input["should"]) == 0:
                # Remove should and minimum_should_match if it's empty
                input.pop("should")
                input.pop("minimum_should_match", {})
            if (
                "minimum_should_match" in input.keys()
                and input["minimum_should_match"] > 1
            ):
                msg = f"Modified minimum_should_match. This logic is immature. Manually check the output. ({self.detection_name})"
                self.logger.warn(msg)
                input["minimum_should_match"] -= 1

            found_tenant_whitelist = True

        return {
            "logic": input,
            "whitelists": removed_items,
            "found_tenant_whitelist": found_tenant_whitelist,
            "split_logic_whitelist": False,
        }

    def _split_logic_should(self, input: dict) -> dict:
        """
        Remove the whitelist condition for a split logic should

        Parameters:
            input: dict
                Object containing the top level query

        Returns:
            dict. Contains the input dict without any tenant whitelisting

        Raises:
            NotImplementedError when unexpected code branch is hit

        Warns:
            when minimum_should_match is modified
        """

        # Do dome setup
        input_copy = deepcopy(input)
        musts = []
        removeds = []

        #  Need to iterate through everything again - as need to find both a must and a must_not
        for i, should in enumerate(input["should"]):
            if "bool" in should.keys():
                s = should["bool"]
                for logic, qs in s.items():
                    for q in qs:
                        if "term" in q.keys():
                            # convert term to terms query
                            te = {}
                            for k, v in q["term"].items():
                                te[k] = [v["value"]]
                            q = {"terms": te}

                        if "terms" in q.keys():
                            if "tenant.name" in q["terms"].keys():
                                if logic == "must":
                                    # earmark to remove later
                                    musts.append(should)
                                elif logic == "must_not":
                                    #  just remove the tenant name
                                    input_copy["should"][i]["bool"][logic].remove(q)
                                else:
                                    raise NotImplementedError("Unexpected code branch")
                            # no else here.

                    if logic == "must_not":
                        # clean up must_not if it's empty
                        if len(input_copy["should"][i]["bool"][logic]) == 0:
                            input_copy["should"][i]["bool"].pop(logic)

        #  remove marked items
        for must in musts:
            removeds.append(must)
            input_copy["should"].remove(must)

        # if there's only one item in should, move it to must
        if len(input_copy["should"]) == 1:
            new_must = input_copy.pop("should")
            if "must" in input_copy.keys():
                input_copy["must"] = input_copy["must"] + new_must
            else:
                input_copy["must"] = new_must

            input_copy.pop("minimum_should_match")

        # Clean minimum_should_match
        if "minimum_should_match" in input_copy.keys():
            if input_copy["minimum_should_match"] > 1:
                msg = f"Modified minimum_should_match. This logic is immature. Manually check the output. ({self.detection_name})"
                self.logger.warn(msg)
                # decrement
                input_copy["minimum_should_match"] -= 1
            if input_copy["minimum_should_match"] > len(input_copy["should"]):
                msg = f"Modified minimum_should_match. This logic is immature. Manually check the output. ({self.detection_name})"
                self.logger.warn(msg)
                # in case we had to move extras
                input_copy["minimum_should_match"] = len(input_copy["should"])

        return {
            "logic": input_copy,
            "whitelists": removeds,
        }

    def prepare_where_field_mapping(self, mapping):
        """
        Converts list mapping of 'source_detection_field_name' to 'target_unified_schema_field_name'
        into a dictionary for faster access.
        """

        field_map = {}
        for m in mapping:
            field_map[m["source_detection_field_name"]] = m[
                "target_unified_schema_field_name"
            ]
        return field_map

    def compute_where_clause(self, row):
        # field_map = self.prepare_where_field_mapping(row['where_fields'])
        return self._convert_es_query_to_sql_where(
            row["es_body"],  # The Elasticsearch query
            file_name=row["filename"],
            field_mappings=row[
                "where_fields"
            ],  # The field mappings for the where clause
            logger=self.logger,
        )

    def compute_cte_with_clause(self, row):
        # This might need to be changed
        # field_map = self.prepare_where_field_mapping(row['where_fields'])

        if row["detection_type"] == "smd":
            return pd.Series([None, None, None, None])
        else:
            cte_with_clause, cte_template, cte_table_name, cte_where_condition = (
                self._convert_es_aggs_to_sql_cte(
                    row["aggs"],
                    row["action_transform_script"],
                    row["action_transform_params"],
                    row["where_clause"]["where"],
                    row["all_terms"],
                    field_mappings=row["where_fields"],
                    logger=self.logger,
                )
            )
        return pd.Series(
            [cte_with_clause, cte_template, cte_table_name, cte_where_condition]
        )

    def _extract_device_types(self, es_indices: Dict[str, Any]) -> Set[str]:
        """
        Converts Elasticsearch indices into a set of device types.

        Args:
        es_indices (Dict[str, Any]): A dictionary containing a list of indices.

        Returns:
        Set[str]: A set of device types extracted from the indices.
        """
        device_types = set()

        for index in es_indices.get("indices", []):
            index_components = index.split("-")

            # Handle generic pattern '*-parsed*' directly
            if len(index_components) < 3 or index_components[0] == "*":
                device_types.add("*")
                continue

            # Adjust index components if they exceed expected segments
            while len(index_components) > 3:
                if index_components[-1] == "*":
                    # Combine the last two components if the last component is a wildcard
                    index_components = index_components[:-2] + [
                        index_components[-2] + "-" + index_components[-1]
                    ]
                else:
                    # Special case handling for specific naming conventions
                    if (
                        index_components[0] == "rbnz"
                        and index_components[1] == "payments"
                    ):
                        index_components = [
                            index_components[0] + "-" + index_components[1]
                        ] + index_components[2:]
                    else:
                        # General case for merging middle components to fit the expected 3-component format
                        index_components = (
                            [index_components[0]]
                            + [index_components[1] + "-" + index_components[2]]
                            + index_components[3:]
                        )

            # Extract device type which is typically the second component of the adjusted index
            device_type = index_components[1]
            device_types.add(device_type)

        return device_types

    def _convert_es_aggs_to_sql_cte(
        self,
        es_aggs: Dict[str, Any],
        transform_script: str,
        transform_script_params: Dict[str, Any],
        generated_where_clause: str,
        all_terms: list,
        field_mappings: Dict[str, str] = None,
        logger: logging.Logger = None,
    ):
        """Convert Elasticsearch aggs blocks to SQL CTE blocks"""

        # Removes un-necessary fields from aggregations block and flattens JSON
        def flatten_nested_dictionary(dictionary, parent_key="", separator="|"):
            flattened_dictionary = {}
            new_key = ""
            for key, value in dictionary.items():
                new_key = f"{parent_key}{separator}{key}" if parent_key else key
                if type(value) == dict:
                    flattened_dictionary.update(
                        flatten_nested_dictionary(value, new_key)
                    )
                elif "field" in new_key and value == "tenant.name":
                    continue
                elif new_key.endswith("field"):
                    flattened_dictionary[new_key] = value
                else:
                    continue
            return flattened_dictionary

        # Updates the aggregation comparison operator and value for all rows
        def add_agg_operator_value(data):
            script_words = transform_script.split()
            for index, word in enumerate(script_words):
                if (
                    index != 0
                    and "params" in word
                    and script_words[index - 1][0] in ["<", ">", "="]
                ):
                    split_value = script_words[index - 2].split(".")
                    for row in data:
                        if (
                            split_value[len(split_value) - 2] in row["agg_name"]
                            or f"{split_value[len(split_value) - 2]}|cardinality"
                            in row["agg_name"]
                        ):
                            found_variable = f"{split_value[len(split_value) - 2]}"
                            break
                        elif split_value[len(split_value) - 5] == "tenant":
                            found_variable = f"{split_value[len(split_value) - 3]}"
                        else:
                            found_variable = f"{split_value[len(split_value) - 5]}|{split_value[len(split_value) - 3]}"
                    for row in data:
                        if f"{found_variable}|cardinality" in row["agg_name"]:
                            variable_name = (
                                script_words[index]
                                .split(".")[1]
                                .replace(")", "")
                                .replace("{", "")
                            )
                            row["agg_operator"] = script_words[index - 1]
                            row["agg_value"] = transform_script_params[variable_name]
                        elif f"{found_variable}" in row["agg_name"]:
                            variable_name = (
                                script_words[index]
                                .split(".")[1]
                                .replace(")", "")
                                .replace("{", "")
                            )
                            row["agg_operator"] = script_words[index - 1]
                            row["agg_value"] = transform_script_params[variable_name]

        flattened_dictionary = flatten_nested_dictionary(es_aggs.get("tenant", {}))

        # Creates blank dataframe
        blank_df_columns = {
            "agg_name": [],
            "agg_field": [],
            "agg_operator": [],
            "agg_value": [],
            "agg_parent": [],
        }
        blank_df = pd.DataFrame(blank_df_columns)
        blank_df["agg_operator"] = blank_df["agg_operator"].astype(str)

        # Loops through flattened dictionary fields, removing un-necessary components and matching to field mappings
        new_df_dictionary = {}
        for key, value in flattened_dictionary.items():
            key = re.sub(r"\b(aggs|terms|field)(\|)?\b", "", key).strip("|")
            key_split = key.split("|")
            # value = sql_column_fix_name(value)
            mapped_fields = get_mapped_field_name(
                value, field_mappings[0], logger, self.native_fields
            )
            if len(key_split) > 1:
                if key_split[len(key_split) - 1] == "cardinality":
                    new_df_dictionary[
                        f"{key_split[len(key_split) - 3]}|{key_split[len(key_split) - 2]}|cardinality"
                    ] = (
                        {"agg_field": mapped_fields},
                        {"agg_parent": key_split[len(key_split) - 3]},
                    )
                else:
                    new_df_dictionary[
                        f"{key_split[len(key_split) - 2]}|{key_split[len(key_split) - 1]}"
                    ] = (
                        {"agg_field": mapped_fields},
                        {"agg_parent": key_split[len(key_split) - 2]},
                    )
            else:
                new_df_dictionary[key] = (
                    {"agg_field": mapped_fields},
                    {"agg_parent": None},
                )

        # Adds the extracted data into the blank dataframe
        data = [
            {
                "agg_name": key,
                "agg_field": value[0]["agg_field"],
                "agg_parent": value[1]["agg_parent"],
            }
            for key, value in new_df_dictionary.items()
        ]
        add_agg_operator_value(data)
        new_df = pd.DataFrame(data)
        df = pd.concat(
            [
                blank_df[
                    ["agg_name", "agg_field", "agg_operator", "agg_value", "agg_parent"]
                ],
                new_df,
            ],
            ignore_index=True,
        )
        self.logger.info(f"CTE dataframe created:\n{df}")

        # Takes the all_terms argument passed in and finds the associated mapping value add adds it to a list of new_terms if it is not aggregated on
        new_terms = []
        all_terms = sorted(all_terms)
        for term in all_terms:
            # term = sql_column_fix_name(term)
            mapped_fields = get_mapped_field_name(
                term, field_mappings[0], logger, self.native_fields
            )
            if mapped_fields not in df["agg_field"].tolist():
                for field in mapped_fields:
                    new_terms.append(f"any({field})")

        # Determines the template to be used for the CTE WITH clause (number of children are required for identification)
        cte_with_clause = "WITH "
        df_children = df[df["agg_parent"].notna()]
        # Aggs on 1 field
        if len(df) == 1:
            template = cte_templates.cte_template_1()
            detection_template = "template 1"
        # Aggs on 2 fields
        elif len(df) == 2:
            # Aggs on field1 AND field2
            if len(df_children) == 1:
                template = cte_templates.cte_template_2()
                detection_template = "template 2"
            # Aggs on field1 OR field2
            elif len(df_children) == 0:
                template = cte_templates.cte_template_3()
                detection_template = "template 3"
            else:
                raise NotImplementedError(
                    f"Detection with {len(df)} DataFrame rows and {len(df_children)} children has not been implemented"
                )
        # Aggs on 2 fields (single field1 AND cardinality field2)
        elif len(df) == 3:
            # Aggs on single field1 and cardinality field2 (cardinality will hold 2 rows)
            if (
                len(df[df["agg_name"].str.contains("cardinality", case=False)]) == 1
                and len(df_children) == 2
            ):
                template = cte_templates.cte_template_4()
                detection_template = "template 4"
            else:
                raise NotImplementedError(
                    f"Detection with {len(df)} DataFrame rows and {len(df[df['agg_name'].str.contains('cardinality', case=False)])} cardinality fields has not been implemented"
                )
        # Aggs on 4 fields ((single field1 AND cardinality fieldA OR fieldB) AND (single field2 AND cardinality fieldA OR fieldB))
        elif len(df) == 10:
            # Aggs on (single field1 AND cardinality fieldA OR fieldB) AND (single field2 AND cardinality fieldA OR fieldB) (each cardinality will hold 2 rows)
            if (
                len(df[df["agg_name"].str.contains("cardinality", case=False)]) == 4
                and len(df_children) == 8
            ):
                template = cte_templates.cte_template_5()
                detection_template = "template 5"
            else:
                raise NotImplementedError(
                    f"Detection with {len(df)} DataFrame rows and {len(df[df['agg_name'].str.contains('cardinality', case=False)])} cardinality fields has not been implemented"
                )
        else:
            raise NotImplementedError(
                f"Detection with {len(df)} DataFrame rows and {len(df[df['agg_name'].str.contains('cardinality', case=False)])} cardinality fields has not been implemented"
            )

        # Define CTE WITH clause variables for global assignment
        main_table_name = ""
        agg1_all_fields, agg1_indent_val, agg1_field = [], 0, ""
        agg2_all_fields, agg2_indent_val, agg2_field1, agg2_field2 = [], 0, "", ""
        (
            agg3_all_fields1,
            agg3_all_fields2,
            agg3_indent_val,
            agg3_field1,
            agg3_field2,
        ) = [], [], 0, "", ""
        agg4_name, agg4_field, agg4_all_fields, agg4_indent_val = "", "", [], 0

        count_conditions = []

        for index, row in df.iterrows():
            # If it only aggregates on 1 field
            if template == cte_templates.cte_template_1():
                main_table_name += "agg1_bucket"
                agg1_field = ", ".join(row.get("agg_field", ""))
                agg1_all_fields = [
                    "any(event_hash) AS event_hash",
                    f"{', '.join(row.get('agg_field', ''))}",
                    f"count() AS {main_table_name}_count",
                ]
                agg1_indent_val = 12
                count_conditions.append(
                    f"{main_table_name}_count {row.get('agg_operator', '')} {int(row.get('agg_value', ''))}"
                )
            # If it aggregates on 2 fields - combined
            elif template == cte_templates.cte_template_2():
                # If the row is the parent row (i.e. aggregation 1)
                if row.get("agg_parent", "") == None:
                    main_table_name += "agg2_bucket"
                    agg2_field1 = ", ".join(row.get("agg_field", ""))
                    agg2_all_fields = [
                        "any(event_hash) AS event_hash",
                        f"{', '.join(row.get('agg_field', ''))}",
                        f"count() AS {main_table_name}_count",
                    ]
                    agg2_indent_val = 12
                # Else, the row has a parent (i.e. aggregation 2)
                else:
                    agg2_field2 = ", ".join(row.get("agg_field", ""))
                    count_conditions += [
                        f"{main_table_name}_count {row.get('agg_operator', '')} {int(row.get('agg_value', ''))}"
                    ]
                    agg2_all_fields.insert(-1, f"{', '.join(row.get('agg_field', ''))}")
            # If it aggregates on 2 fields - separate
            elif template == cte_templates.cte_template_3():
                # If the row is the first aggregation
                if index == 0:
                    main_table_name += "agg3_bucket"
                    agg3_field1 = ", ".join(row.get("agg_field", ""))
                    agg3_all_fields1 = [
                        "any(event_hash) AS event_hash",
                        f"{', '.join(row.get('agg_field', ''))}",
                        f"count() AS {main_table_name}_count",
                    ]
                    agg3_indent_val = 12
                    count_conditions += [
                        f"{main_table_name}_count {row.get('agg_operator', '')} {int(row.get('agg_value', ''))}"
                    ]
                # Else, the row is the second aggregation
                else:
                    agg3_field2 = ", ".join(row.get("agg_field", ""))
                    agg3_all_fields2 = [
                        "any(event_hash) AS event_hash",
                        f"{', '.join(row.get('agg_field', ''))}",
                        f"count() AS {main_table_name}_count",
                    ]
            # If it aggregates on 2 fields - single field1 AND cardinality field2
            elif template == cte_templates.cte_template_4():
                # If the row is the parent row (i.e. aggregation 1)
                if row.get("agg_parent", "") == None:
                    main_table_name += "agg4_bucket"
                    agg4_field = ", ".join(row.get("agg_field", ""))
                    agg4_name = row.get("agg_name", "").replace("|cardinality", "")
                    child_row = df[df["agg_parent"] == agg4_name]
                # Else, if the row does not have the name 'cardinality' ('cardinality' row will get skipped)
                elif "cardinality" not in row.get("agg_name", ""):
                    agg4_all_fields = [
                        "any(event_hash) AS event_hash",
                        f"{agg4_field}",
                        f"count(DISTINCT {', '.join(row.get('agg_field', ''))}) AS unique_{', '.join(row.get('agg_field', ''))}s",
                    ]
                    agg4_indent_val = 12
                else:
                    for field in child_row.get("agg_field", "").iloc[0]:
                        count_conditions += [
                            f"{main_table_name}.unique_{', '.join(row.get('agg_field', ''))}s {row.get('agg_operator', '')} {int(row.get('agg_value', ''))}"
                        ]
            # # If it aggregates on 6 fields - (single field1 AND cardinality fieldA OR fieldB) AND (single field2 AND cardinality fieldA OR fieldB)
            # elif (template == cte_templates.cte_template_5() or template == '\n'.join(cte_templates.cte_template_5().split('\n')[1:])):
            #     # If the row is a parent row
            #     if (row.get("agg_parent", '') == None and not('|' in row.get("agg_name"))):
            #         # If the parent value is already populated (i.e. this is field2), add the formatted template and reset variables
            #         if (agg1_name != ""):
            #             cte_with_clause += f"{'\n'.join(template.rstrip().split('\n')[:-1]).format(
            #                 main_table_name = '{cte_table_name}',
            #                 agg1_name = agg1_name,
            #                 agg1_all_fields = f",\n{' ' * agg1_indent_val}".join(agg1_all_fields),
            #                 org_id = '{org_id}',
            #                 source_table = '{source_table}',
            #                 where_clause = generated_where_clause,
            #                 agg1_field = agg1_field,
            #                 agg2_all_fields = f",\n{' ' * agg2_indent_val}".join(agg2_all_fields),
            #                 agg4_all_fields = f",\n{' ' * agg2_indent_val}".join(agg4_all_fields),
            #                 agg2_field = agg2_field,
            #                 agg4_field = agg4_field,
            #                 agg1_fields = agg1_fields,
            #                 condition = '{condition}',
            #                 having_condition = " OR ".join(count_conditions),
            #                 agg2_fields = ", ".join(agg2_fields)
            #             ).rstrip()}\n{' ' * 8}UNION ALL\n"
            #             agg1_name, agg1_field, agg1_all_fields, agg1_indent_val = "", "", [], 0
            #             agg2_name, agg2_parent, agg2_field, agg2_all_fields, agg2_indent_val = "", "", "", [], 0
            #             agg1_fields, agg2_fields = [], []
            #             count_conditions = []
            #             template = '\n'.join(template.split('\n')[1:])
            #         agg1_name = row.get("agg_name", '')
            #         main_table_name = f"{agg1_name.split('|')[-1]}_{main_table_name}" if (len(main_table_name) != 0) else f"{agg1_name}"
            #         agg1_field = " OR ".join(row.get("agg_field", ''))
            #         agg1_all_fields = [
            #             f"any(timestamp) AS {agg1_name}_timestamp",
            #             f"any(logoriginal) AS {agg1_name}_logoriginal"
            #         ] + new_terms
            #         for field in row.get("agg_field", ''):
            #             agg1_all_fields.append(field)
            #         agg1_indent_val = 16
            #     elif ("cardinality" not in row.get("agg_name", '')):
            #         agg2_name = row.get("agg_name", '')
            #         main_table_name = f"{agg2_name.split('|')[-1]}_{main_table_name}"
            #         agg2_field = " OR ".join(row.get("agg_field", ''))
            #         agg2_parent = row.get("agg_parent", '')
            #         for field in row.get("agg_field", ''):
            #             agg1_all_fields += [f"any({field}) AS {agg2_parent}_{field}"]
            #             agg2_fields += [f"{agg2_parent}_{field}"]
            #             if (len(agg2_all_fields) == 0):
            #                 agg2_all_fields = [
            #                     f"count({agg2_parent}_{field}) AS {agg2_parent}_{field}_count",
            #                     f"{agg2_parent}_{field}"
            #                 ]
            #             else:
            #                 agg2_all_fields = [
            #                     f"any({agg2_parent}_timestamp) AS {{cte_table_name}}_timestamp",
            #                     f"any({agg2_parent}_logoriginal) AS {{cte_table_name}}_logoriginal"
            #                     ] + agg2_all_fields + [
            #                     f"count({agg2_parent}_{field}) AS {agg2_parent}_{field}_count",
            #                     f"{agg2_parent}_{field}"
            #                 ]
            #         agg2_indent_val = 12
            #     else:
            #         agg2_field = " OR ".join(row.get("agg_field", ''))
            #         agg2_parent = row.get("agg_parent", '')
            #         for field in row.get("agg_field", ''):
            #             count_conditions += [f"{agg2_parent}_{field}_count {row.get("agg_operator", '')} {int(row.get("agg_value", ''))}"]

        # Format the selected template with required values
        cte_with_clause += template.format(
            main_table_name=main_table_name,
            agg1_all_fields=f",\n{' ' * agg1_indent_val}".join(agg1_all_fields),
            agg2_all_fields=f",\n{' ' * agg2_indent_val}".join(agg2_all_fields),
            agg3_all_fields1=f",\n{' ' * agg3_indent_val}".join(agg3_all_fields1),
            agg3_all_fields2=f",\n{' ' * agg3_indent_val}".join(agg3_all_fields2),
            agg4_all_fields=f",\n{' ' * agg4_indent_val}".join(agg4_all_fields),
            # agg5_all_fields = f",\n{' ' * agg5_indent_val}".join(agg5_all_fields),
            org_id="{org_id}",
            source_table="{source_table}",
            where_clause=generated_where_clause.replace("{", "{{").replace("}", "}}"),
            agg1_field=agg1_field,
            agg2_field1=agg2_field1,
            agg2_field2=agg2_field2,
            agg3_field1=agg3_field1,
            agg3_field2=agg3_field2,
            agg4_field=agg4_field,
        )

        if "{having_condition}" in template:
            count_conditions = []

        # Create the WHERE condition based off the count_conditions list
        cte_where_condition = f"{' OR '.join(count_conditions)}"

        return cte_with_clause, detection_template, main_table_name, cte_where_condition

    def parse_watchers(self, watcher_path: str) -> pd.DataFrame:
        self.logger.info(f"Parsing Watchers from this Directory: {watcher_path}")
        watcher_directories = self._return_watchers_directories(watcher_path)

        self.logger.info(f"Parsed watcher_directories: {watcher_directories}")

        raw_df = self._process_directories(watcher_directories)

        self.logger.info(f"Number of Rows: {raw_df.shape[0]}")
        self.logger.info(f"Number of Cols: {raw_df.shape[1]}")
        self.logger.info(f"Cols: {raw_df.columns}")

        es_watchers_dsl_df = raw_df.applymap(
            lambda x: x.strip() if type(x) == str else x
        )
        es_watchers_dsl_df = es_watchers_dsl_df[
            [
                "filename",
                "schedule",
                "indices",
                "filter_range",
                "must_terms",
                "must_not_terms",
                "should_terms",
                "aggs",
                "condition_script",
                "action_transform_script",
                "action_transform_params",
                "action_index",
                "es_body",
                "schedule_duration",
                "detection_type",
                "description",
                "title",
                "tags",
                "name",
                "watcher_id",
                "schedule_group",
            ]
        ]

        all_terms = (
            es_watchers_dsl_df["must_terms"]
            + es_watchers_dsl_df["must_not_terms"]
            + es_watchers_dsl_df["should_terms"]
        )
        unique_values = []
        for index, terms in enumerate(all_terms):
            unique_values.append([])
            for subterm in terms:
                if subterm not in unique_values[index]:
                    unique_values[index].append(subterm)

        es_watchers_dsl_df["all_terms"] = unique_values
        es_watchers_dsl_df["unique_terms_count"] = es_watchers_dsl_df[
            "all_terms"
        ].apply(lambda x: len(set(x)))
        es_watchers_dsl_df["used_terms_count"] = es_watchers_dsl_df["all_terms"].apply(
            lambda x: len(list(x))
        )
        es_watchers_dsl_df["used_terms_count"] = es_watchers_dsl_df["all_terms"].apply(
            lambda x: len(list(x))
        )

        es_watchers_dsl_df["device_customer_map"] = es_watchers_dsl_df["indices"].apply(
            lambda x: self._extract_device_and_customer_from_indices({"indices": x})
        )
        es_watchers_dsl_df["device_types"] = es_watchers_dsl_df["indices"].apply(
            lambda x: set(self._extract_device_types({"indices": x}))
        )
        es_watchers_dsl_df["select_fields"] = es_watchers_dsl_df[
            "device_customer_map"
        ].apply(lambda x: self._map_fields(self.schema_maps["select_field_map"], x))

        es_watchers_dsl_df["where_fields"] = es_watchers_dsl_df[
            "device_customer_map"
        ].apply(lambda x: self._map_fields(self.schema_maps["where_field_map"], x))

        es_watchers_dsl_df["where_clause"] = es_watchers_dsl_df.apply(
            lambda row: self.compute_where_clause(row), axis=1
        )

        es_watchers_dsl_df[
            ["cte_with_clause", "cte_template", "cte_table_name", "cte_where_condition"]
        ] = es_watchers_dsl_df.apply(
            lambda row: self.compute_cte_with_clause(row), axis=1
        )

        es_watchers_dsl_df = self._parse_action_transform_scripts(
            es_watchers_dsl_df, "action_transform_script"
        )

        self.logger.info(
            f"es_watchers_dsl_df Number of Rows: {es_watchers_dsl_df.shape[0]}"
        )
        self.logger.info(
            f"es_watchers_dsl_df Number of Cols: {es_watchers_dsl_df.shape[1]}"
        )
        self.logger.info(f"es_watchers_dsl_df Cols: {es_watchers_dsl_df.columns}")
        self.logger.info(
            f"es_watchers_dsl_df - where fields Elastic detections:{self.unique_fields} \n"
        )

        return es_watchers_dsl_df

    def _map_fields(self, field_map, device_types):
        """Retrieve field mappings based on the device types extracted."""
        fields = []
        for device_type in device_types:
            if device_type in field_map:
                fields.append(field_map[device_type])
        return fields
