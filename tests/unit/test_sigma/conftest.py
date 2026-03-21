import os
import shutil

import pandas as pd
import pytest
import yaml


def _create_base_meta_schema_nxlog_windows():
    """Create a base meta schema DataFrame for nxlog windows with sigma field mappings."""
    columns = [
        "event_id",
        "domain",
        "event_creation_time",
        "category",
        "channel",
        "description",
        "event_type",
        "hostname",
        "ip_address",
        "level",
        "message",
        "process_id",
        "process_name",
        "command_line",
        "parent_process_name",
        "parent_process_id",
        "parent_command_line",
        "process_hash",
    ]

    types = [
        "string",
        "string_fast_lowcardinality",
        "datetime",
        "string_fast_lowcardinality",
        "string_fast_lowcardinality",
        "string_fast_lowcardinality",
        "string_fast_lowcardinality",
        "string_fast_lowcardinality",
        "string_fast_lowcardinality",
        "string_fast_lowcardinality",
        "text",
        "string",
        "string",
        "string",
        "string",
        "string",
        "string",
        "string",
    ]

    index_orders = [
        "2",
        "",
        "",
        "0",
        "1",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    ]
    index_types = [""] * len(columns)
    os_orders = [""] * len(columns)
    comments = [
        "Windows Event Log ID",
        "Domain",
        "Event Creation Time",
        "Category",
        "Channel",
        "Description",
        "Event Type",
        "Hostname",
        "IP Address",
        "Level",
        "Message",
        "Process ID",
        "Process Name",
        "Command Line",
        "Parent Process Name",
        "Parent Process ID",
        "Parent Command Line",
        "Process Hash",
    ]

    sigma_fields = [
        "SigmaRule_EventId",
        "",
        "",
        "",
        "SigmaRule_Channel",
        "",
        "",
        "",
        "",
        "",
        "",
        "SigmaRule_ProcessId",
        "SigmaRule_Image",
        "SigmaRule_CommandLine",
        "SigmaRule_ParentImage",
        "SigmaRule_ParentProcessId",
        "SigmaRule_ParentCommandLine",
        "SigmaRule_ProcessHash",
    ]

    return pd.DataFrame(
        {
            "column": columns,
            "type": types,
            "index_order": index_orders,
            "index_type": index_types,
            "os_order": os_orders,
            "comment": comments,
            "sigma_field_name": sigma_fields,
        }
    )


@pytest.fixture(scope="session")
def dfe_config_fixtures(tmp_path_factory):
    """Create test configuration with proper paths."""
    temp_dir = tmp_path_factory.mktemp("test_dfe_schema_output")

    targets_dir = tmp_path_factory.mktemp("test_targets")
    targets_path = targets_dir / "dfe_targets.yaml"
    targets_data = {
        "default_target": "test_schemas",
        "targets": {
            "test_schemas": {
                "ch_host": "localhost",
                "ch_port": 9440,
                "ch_username": "",
                "ch_password": "",
            }
        },
    }

    with open(targets_path, "w") as f:
        yaml.dump(targets_data, f)

    post_build_path = tmp_path_factory.mktemp("post_build_artefacts")
    meta_schemas_path = post_build_path / "dfe_meta_schemas"
    nxlog_path = meta_schemas_path / "logs_nxlog_windows" / "v001_000_004"
    nxlog_path.mkdir(parents=True, exist_ok=True)

    schema_df = _create_base_meta_schema_nxlog_windows()
    schema_file = nxlog_path / "logs_nxlog_windows.csv"
    schema_df.to_csv(schema_file, index=False)

    config_data = {
        "global_settings": {
            "schema_common_version": "v001.001.007",
            "schema_output_path": str(temp_dir),
            "default_target": "test_schemas",
            "target_path": str(targets_path),
            "vector_files": {
                "standard": [
                    "ingestion_pipeline_enrichment_standard",
                    "ingestion_pipeline_enrichment_standard_custom",
                ],
                "geoip": ["ingestion_pipeline_enrichment_geoip"],
                "vector": ["ingestion_pipeline_templates"],
            },
            "derived_schema_paths": "tests/resources/test_schemas/stable_schemas",
            "meta_schema_paths": str(post_build_path / "dfe_meta_schemas"),
        },
        "schemas": {
            "alert_field_defaults": {
                "alert_schedule": "smd",
                "alert_schedule_duration": "10mins",
                "alert_ratingtime_sla_applies": "true",
                "alert_framework": "MITRE ATT&CK",
            },
            "source_field_mappings": {
                "Image": "process.name",
                "CommandLine": "process.command_line",
                "ParentImage": "process.parent.name",
                "ProcessId": "process.pid",
                "ParentProcessId": "process.parent.pid",
            },
            "logs_alerts": {
                "name": "logs_alerts",
                "meta_schema": "logs_alerts.csv",
                "meta_schema_version": "v001.000.000",
                "derived_schema_file_path": "logs_alerts/logs_alerts_sub.csv",
                "additional_fields_config": "logs_alerts/logs_alerts_add.csv",
                "sigma_rules": {
                    "suspicious_powershell": {
                        "path": "windows/process_creation/suspicious_powershell.yml",
                        "alert_fields": {
                            "process_name": "Image",
                            "command_line": "CommandLine",
                        },
                        "alert_metadata": {
                            "alert_type": "Suspicious PowerShell",
                            "alert_severity": "medium",
                            "alert_triage_score": 40,
                        },
                    }
                },
            },
            "logs_nxlog_windows": {
                "name": "logs_nxlog_windows",
                "meta_schema": "logs_nxlog_windows.csv",
                "meta_schema_version": "v001.000.004",
                "derived_schema_file_path": "logs_nxlog_windows/logs_nxlog_windows_sub.csv",
                "additional_fields_config": "logs_nxlog_windows/logs_nxlog_windows_add.csv",
                "derived_schema_ttl": 90,
                "sigma_rules": {
                    "suspicious_process": {
                        "path": "windows/process_creation/suspicious_process.yml",
                        "alert_fields": {
                            "process_name": "Image",
                            "command_line": "CommandLine",
                            "parent_process_name": "ParentImage",
                        },
                        "alert_metadata": {
                            "alert_type": "Suspicious Process",
                            "alert_severity": "high",
                            "alert_triage_score": 60,
                        },
                    }
                },
            },
        },
    }

    return config_data


@pytest.fixture(scope="session")
def setup_paths(tmp_path_factory):
    """Create and clean up log paths."""
    dfe_root_log_path = tmp_path_factory.mktemp("logs_path")
    yield str(dfe_root_log_path)
    if os.path.exists(dfe_root_log_path):
        shutil.rmtree(dfe_root_log_path)


@pytest.fixture
def text_schema_content():
    """CSV content for test schema with text field."""
    return """column,type,index_type,sigma_field_name
              message,text,default,message
            """


@pytest.fixture
def text_schema_config(tmp_path):
    """Create test schema configuration with text field."""
    meta_schema_dir = tmp_path / "meta_schemas" / "test_meta" / "v001_000_000"
    meta_schema_dir.mkdir(parents=True)
    meta_schema_file = meta_schema_dir / "test_meta.csv"
    meta_schema_file.write_text("""column,type,index_type,sigma_field_name
message,text,default,message""")

    return {
        "global_settings": {
            "meta_schema_paths": str(tmp_path / "meta_schemas"),
            "derived_schema_paths": str(tmp_path / "derived_schemas"),
        },
        "schemas": {
            "test_schema1": {
                "meta_schema": "test_meta.csv",
                "meta_schema_version": "v001.000.000",
                "sigma_rules": {
                    "test_warning": {
                        "path": "warning_test.yml",
                        "alert_fields": {"message": "message"},
                    }
                },
            }
        },
    }


@pytest.fixture(scope="session")
def dfe_package(dfe_config_fixtures, tmp_path_factory):
    """Create and clean up test package file."""
    temp_dir = tmp_path_factory.mktemp("test_package")
    dfe_package_path = temp_dir / "dfe_package_testing.yaml"

    with open(dfe_package_path, "w") as f:
        yaml.dump(dfe_config_fixtures, f)

    yield str(dfe_package_path)
    if os.path.exists(dfe_package_path):
        os.remove(dfe_package_path)
