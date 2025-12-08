import pytest
import os
import yaml
import logging


@pytest.fixture
def common_template_package():
    return "dfe_engine.resources.common"


@pytest.fixture
def common_resource_version():
    return "v001_001_005"


@pytest.fixture
def schema_template_package_path():
    return "resources.dfe_schemas_templates"


@pytest.fixture(scope="session")
def dfe_config_fixtures():
    config_data = {
        "global_settings": {
            "schema_common_version": "v001.001.007",
            "derived_schema_paths": "tests/resources/test_schemas/stable_schemas",
            "schema_output_path": "../.dfe_schema_output/",
            "default_target": "integration",
            "target_path": "~/.dfe/dfe_targets.yaml",
            "vector_files": {
                "standard": [
                    "../../post_build_artefacts/dfe_meta_schemas_package/ingestion_pipeline_enrichment_standard",
                    "../../post_build_artefacts/dfe_meta_schemas_package/ingestion_pipeline_enrichment_standard_custom",
                ],
                "geoip": [
                    "../../post_build_artefacts/dfe_meta_schemas_package/ingestion_pipeline_enrichment_geoip"
                ],
            },
            "vector_files": {
                "vector": [
                    "../../post_build_artefacts/dfe_meta_schemas_package/ingestion_pipeline_templates"
                ]
            },
            "meta_schema_paths": "../../post_build_artefacts/dfe_meta_schemas_package/dfe_meta_schemas",
        },
        "build_schemas": {
            "no_cluster_declarations_needed": True,
            "use_replicated_merge_tree": False,
            "use_shared_merge_tree": False,
        },
        "apply_schemas": {
            "do_add_roles": False,
            "do_add_columns": True,
        },
        "organisations": [
            {"org_id": "detectionlab", "cluster_name": ""},
            {"org_id": "org321", "cluster_name": ""},
            {"org_id": "org111111", "cluster_name": ""},
        ],
        "schemas": {
            "logs_alerts": {
                "name": "logs_alerts",
                "meta_schema": "logs_alerts.csv",
                "meta_schema_version": "v001.000.000",
            },
            "nx_log_windows_sub_ce": {
                "name": "nx_log_windows_sub_ce",
                "meta_schema": "logs_nxlog_windows.csv",
                "meta_schema_version": "v001.000.003",
                "derived_schema_file_path": "nx_log_windows_sub_ce/nx_log_windows_sub_ce.csv",
                "additional_fields_config": "nx_log_windows_sub_ce/nx_log_windows_ce_add_fields.csv",
                "unified_schema_mapping": "nx_log_windows_sub_ce/unified_schema_mapping.csv",
                "derived_schema_ttl": 90,
            },
            "log_windows_unittest_sub": {
                "name": "log_windows_unittest_sub",
                "meta_schema": "logs_nxlog_windows.csv",
                "meta_schema_version": "v001.000.003",
                "derived_schema_file_path": "log_windows_unittest_sub/log_windows_unittest_sub.csv",
                "additional_fields_config": "log_windows_unittest_sub/log_windows_unittest_add_fields.csv",
                "derived_schema_ttl": 90,
            },
            "log_windows_unittest1_sub": {
                "name": "log_windows_unittest1_sub",
                "meta_schema": "logs_nxlog_windows.csv",
                "meta_schema_version": "v001.000.003",
                "derived_schema_file_path": "log_windows_unittest1_sub/log_windows_unittest1_sub.csv",
                "additional_fields_config": "log_windows_unittest1_sub/log_windows_unittest1_add_fields.csv",
                "derived_schema_ttl": 90,
            },
            "log_windows_unittest2_sub": {
                "name": "log_windows_unittest2_sub",
                "meta_schema": "logs_nxlog_windows.csv",
                "meta_schema_version": "v001.000.003",
                "derived_schema_file_path": "log_windows_unittest2_sub/log_windows_unittest2_sub.csv",
                "additional_fields_config": "log_windows_unittest2_sub/log_windows_unittest2_add_fields.csv",
                "derived_schema_ttl": 90,
            },
            "logs_nxlog_windows": {
                "name": "logs_nxlog_windows",
                "meta_schema": "logs_nxlog_windows.csv",
                "meta_schema_version": "v001.000.003",
                "derived_schema_file_path": "logs_nxlog_windows/logs_nxlog_windows_sub.csv",
                "additional_fields_config": "logs_nxlog_windows/logs_nxlog_windows_add.csv",
                "derived_schema_ttl": 90,
            },
            "logs_beats_filebeat_igmp": {
                "name": "logs_beats_filebeat_igmp",
                "meta_schema": "logs_beats_filebeat.json",
                "meta_schema_version": "v001.000.002",
                "derived_schema_file_path": "logs_beats_filebeat/logs_beats_filebeat_igmp.csv",
                "derived_schema_ttl": 90,
            },
        },
    }

    return config_data


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)8s | %(module)20s:%(lineno)4d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

test_stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "test_files": {}}


def pytest_runtest_logreport(report):
    """Collect test statistics."""
    if report.when == "call":
        test_stats["total"] += 1

        test_file = report.nodeid.split("::")[0]
        if test_file not in test_stats["test_files"]:
            test_stats["test_files"][test_file] = {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
            }

        if report.passed:
            test_stats["passed"] += 1
            test_stats["test_files"][test_file]["passed"] += 1
        elif report.failed:
            test_stats["failed"] += 1
            test_stats["test_files"][test_file]["failed"] += 1
        elif report.skipped:
            test_stats["skipped"] += 1
            test_stats["test_files"][test_file]["skipped"] += 1

        test_stats["test_files"][test_file]["total"] += 1


def pytest_terminal_summary(terminalreporter, exitstatus):
    """Print test summary at the end."""
    logger.info(
        "\n===================== test_schema_packaging Test Summary ====================="
    )
    logger.info("Overall Statistics:")
    logger.info(f"Total Tests Run: {test_stats['total']}")
    logger.info(f"Tests Passed:    {test_stats['passed']}")
    logger.info(f"Tests Failed:    {test_stats['failed']}")
    logger.info(f"Tests Skipped:   {test_stats['skipped']}")
    logger.info("\nBreakdown by Test File:")

    for test_file, stats in test_stats["test_files"].items():
        logger.info(f"\n{test_file}:")
        logger.info(f"  Total Tests: {stats['total']}")
        logger.info(f"  Passed:      {stats['passed']}")
        logger.info(f"  Failed:      {stats['failed']}")
        logger.info(f"  Skipped:     {stats['skipped']}")

    logger.info("=================================================================")


@pytest.fixture
def opensearch_test_template_input():
    """Test input for OpenSearch template generation."""
    return {
        "template": {
            "mappings": {
                "properties": {
                    "user_agent": {
                        "properties": {
                            "os": {
                                "properties": {
                                    "name": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                        "properties": {
                                            "fields": {
                                                "properties": {
                                                    "text": {
                                                        "type": "keyword",
                                                        "ignore_above": 1024,
                                                        "normalizer": "lowercase_normalizer",
                                                    }
                                                }
                                            }
                                        },
                                    },
                                    "version": {
                                        "type": "keyword",
                                        "ignore_above": 1024,
                                        "normalizer": "lowercase_normalizer",
                                    },
                                }
                            }
                        }
                    }
                }
            }
        }
    }


@pytest.fixture(scope="session")
def dfe_package(dfe_config_fixtures):
    dfe_package_path = os.path.join(os.getcwd(), "dfe_package_testing.yaml")

    with open(dfe_package_path, "w") as f:
        yaml.dump(dfe_config_fixtures, f)

    return dfe_package_path


@pytest.fixture(scope="session")
def expected_filebeat_cm_template_json():
    cm_template_json = """ 
    {
    "template": {
        "mappings": {
        "date_detection": false,
        "properties": {
            "@timestamp": {
            "type": "date"
            },
            "timestamp_load": {
            "type": "date"
            },
            "event_hash": {
            "type": "keyword",
            "ignore_above": 1024,
            "normalizer": "lowercase_normalizer"
            },
            "logoriginal": {
            "type": "keyword",
            "norms": false,
            "ignore_above": 1024,
            "normalizer": "lowercase_normalizer"
            },
            "org_id": {
            "type": "keyword",
            "ignore_above": 1024,
            "normalizer": "lowercase_normalizer"
            },
            "tags": {
            "type": "keyword",
            "ignore_above": 1024,
            "normalizer": "lowercase_normalizer"
            },
            "metadata": {
            "type": "object"
            },
            "azure": {
            "properties": {
                "tenant_id": {
                "type": "keyword",
                "ignore_above": 1024,
                "normalizer": "lowercase_normalizer"
                },
                "auditlogs": {
                "properties": {
                    "operation_name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "tenant_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "operation_version": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "identity": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "result_signature": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "initiated_by": {
                    "properties": {
                        "app": {
                        "properties": {
                            "serviceprincipalname": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            },
                            "displayname": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            },
                            "appid": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            },
                            "serviceprincipalid": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            }
                        }
                        },
                        "user": {
                        "properties": {
                            "displayname": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            },
                            "ipaddress": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            },
                            "id": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            },
                            "userprincipalname": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            }
                        }
                        }
                    }
                    },
                    "logged_by_service": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "result": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "activity_display_name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "operation_type": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "correlation_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "activity_datetime": {
                    "type": "date"
                    },
                    "id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "target_resources": {
                    "properties": {
                        "user_principal_name": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        },
                        "modified_properties": {
                        "properties": {
                            "old_value": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            },
                            "display_name": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            },
                            "new_value": {
                            "type": "keyword",
                            "ignore_above": 1024,
                            "normalizer": "lowercase_normalizer"
                            }
                        }
                        },
                        "ip_address": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        },
                        "id": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        },
                        "type": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        },
                        "display_name": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        }
                    }
                    },
                    "category": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "result_reason": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    }
                }
                },
                "consumer_group": {
                "type": "keyword",
                "ignore_above": 1024,
                "normalizer": "lowercase_normalizer"
                },
                "offset": {
                "type": "long"
                },
                "resource": {
                "properties": {
                    "provider": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "namespace": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "authorization_rule": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "group": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    }
                }
                },
                "eventhub": {
                "type": "keyword",
                "ignore_above": 1024,
                "normalizer": "lowercase_normalizer"
                },
                "enqueued_time": {
                "type": "date"
                },
                "subscription_id": {
                "type": "keyword",
                "ignore_above": 1024,
                "normalizer": "lowercase_normalizer"
                },
                "sequence_number": {
                "type": "long"
                },
                "partition_id": {
                "type": "long"
                },
                "platformlogs": {
                "type": "object",
                "properties": {
                    "status": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    }
                }
                },
                "signinlogs": {
                "properties": {
                    "tenant_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "operation_name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "result_type": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "result_description": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "operation_version": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "identity": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "category": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "result_signature": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "risk_level_aggregated": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "is_tenant_restricted": {
                    "type": "boolean"
                    },
                    "risk_event_types": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "authentication_requirement_policies": {
                    "type": "object"
                    },
                    "sso_extension_version": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "client_app_used": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "resource_tenant_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "risk_level_during_signin": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "created_at": {
                    "type": "date"
                    },
                    "authentication_protocol": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "token_issuer_type": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "user_type": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "original_request_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "conditional_access_status": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "authentication_requirement": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "app_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "risk_event_types_v2": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "is_interactive": {
                    "type": "boolean"
                    },
                    "service_principal_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "flagged_for_review": {
                    "type": "boolean"
                    },
                    "home_tenant_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "app_display_name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "authentication_processing_details": {
                    "type": "object"
                    },
                    "device_detail": {
                    "properties": {
                        "device_id": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        },
                        "browser": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        },
                        "operating_system": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        },
                        "is_compliant": {
                        "type": "boolean"
                        },
                        "trust_type": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        },
                        "display_name": {
                        "type": "keyword",
                        "ignore_above": 1024,
                        "normalizer": "lowercase_normalizer"
                        },
                        "is_managed": {
                        "type": "boolean"
                        }
                    }
                    },
                    "risk_detail": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "resource_display_name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "autonomous_system_number": {
                    "type": "long"
                    },
                    "token_issuer_name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "risk_state": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "incoming_token_type": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "cross_tenant_access_type": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "user_principal_name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "processing_time_ms": {
                    "type": "float"
                    },
                    "user_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "resource_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "correlation_id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "service_principal_name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "user_display_name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "unique_token_identifier": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "status": {
                    "properties": {
                        "error_code": {
                        "type": "long"
                        }
                    }
                    }
                }
                },
                "correlation_id": {
                "type": "keyword",
                "ignore_above": 1024,
                "normalizer": "lowercase_normalizer"
                },
                "activitylogs": {
                "type": "object"
                }
            }
            },
            "o365": {
            "properties": {
                "audit": {
                "properties": {
                    "itemtype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "userkey": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "templatetypeid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "alertentityid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "eventdata": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "item": {
                    "type": "object"
                    },
                    "mailboxownerupn": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "correlationid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "uniquesharingid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "status": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "parameters": {
                    "type": "object"
                    },
                    "externalaccess": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "sourcefileextension": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "internallogontype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "appid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "targetuserorgrouptype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "creationtime": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "id": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "listbasetemplatetype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "applicationid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "fromapp": {
                    "type": "boolean"
                    },
                    "usertype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "members": {
                    "type": "object"
                    },
                    "listbasetype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "site": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "azureactivedirectoryeventtype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "actoruserid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "actoripaddress": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "actoryammeruserid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "clientipaddress": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "extendedproperties": {
                    "type": "object"
                    },
                    "modifiedproperties": {
                    "type": "object"
                    },
                    "logonerror": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "datatype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "itemname": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "listicon": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "actorcontextid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "resultstatus": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "applicationdisplayname": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "workload": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "sourcerelativeurl": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "exceptioninfo": {
                    "type": "object"
                    },
                    "policyid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "groupname": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "targetuserorgroupname": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "implicitshare": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "name": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "intrasystemid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "originatingserver": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "version": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "webid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "clientappid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "sharepointmetadata": {
                    "type": "object"
                    },
                    "donotdistributeevent": {
                    "type": "boolean"
                    },
                    "sessionid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "mailboxownermasteraccountsid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "sourcefilename": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "listcolor": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "clientip": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "entitytype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "eventsource": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "intersystemsid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "aadgroupid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "organizationname": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "category": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "teamguid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "mailboxguid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "operation": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "organizationid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "teamname": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "source": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "alerttype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "logonusersid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "recordtype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "listid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "supportticketid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "clientinfostring": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "useragent": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "itemcount": {
                    "type": "long"
                    },
                    "customuniqueid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "listitemuniqueid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "isdoclib": {
                    "type": "boolean"
                    },
                    "mailboxownersid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "objectid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "comments": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "listtitle": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "yammernetworkid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "logontype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "errornumber": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "severity": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "data": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "siteurl": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "incidentid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "communicationtype": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "sensitiveinfodetectionisincluded": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "alertid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "userid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "targetcontextid": {
                    "type": "keyword",
                    "ignore_above": 1024,
                    "normalizer": "lowercase_normalizer"
                    },
                    "exchangemetadata": {
                    "type": "object"
                    }
                }
                }
            }
            },
            "message": {
            "type": "keyword",
            "ignore_above": 1024,
            "normalizer": "lowercase_normalizer"
            }
        }
        },
        "settings": {
        "index": {
            "query": {
            "default_field": [
                "event_hash",
                "message",
                "logoriginal"
            ]
            }
        }
        }
    },
    "index": {
        "patterns": [
        "logs-beats-filebeat-small.json*"
        ]
    },
    "_meta": {
        "description": "HyperSec logs_beats_filebeat_small OpenSearch ISM log streaming template. COMPATIBILITY MODE. v2.3"
    },
    "composed_of": [
        "hypersec-log-component-template"
    ],
    "priority": "100",
    "data_stream": {
        "timestamp_field": {
        "name": "@timestamp"
        }
    },
    "index_patterns": [
        "logs-beats-filebeat-small*"
    ]
    }
    """

    return cm_template_json
