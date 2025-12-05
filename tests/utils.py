import os
import yaml
from pathlib import Path


class TestConfigBuilder:
    @staticmethod
    def create_test_dfe_package(tmp_path_factory) -> tuple[dict, str]:
        """
        Creates a test dfe_package.yaml file that references templates from post_build_artefacts.

        Args:
            tmp_path_factory: pytest's tmp_path_factory fixture

        Returns:
            tuple: (config_dict, path_to_yaml_file)
        """
        temp_dir = tmp_path_factory.mktemp("test_output")

        # Get the absolute path to post_build_artefacts directory
        post_build_path = (
            Path(__file__).parent
            / "resources"
            / "post_build_artefacts"
            / "ingestion_pipeline"
        )

        config_data = {
            "global_settings": {
                "schema_common_version": "v001.001.007",
                "schema_output_path": str(temp_dir),
                "output": str(temp_dir / "ingestion_pipelines"),
                "upload_ingestion_template_output_path": str(
                    temp_dir / "upload_templates"
                ),
                "default_target": "integration",
                "target_path": str(temp_dir / "dfe_targets.yaml"),
                "vector_files": {
                    "standard": [str(post_build_path / "standard_mappings")],
                    "geoip": [str(post_build_path / "geoip_mappings")],
                },
                "vector_files": {
                    "vector": [str(post_build_path / "core_templates")]
                },
            },
            "vector_templates": {
                "core": [
                    {"name": "000-source-file.yml", "version": "v001.000.000"},
                    {
                        "name": "101-transform-flatten-message.yml",
                        "version": "v001.000.000",
                    },
                    {
                        "name": "104-transform-extract-fields.yml",
                        "version": "v001.000.000",
                    },
                ]
            },
            "ip_config_standard_enrichment": [
                {
                    "name": "hypersec-enrichment-ch-array-fields.csv",
                    "version": "v001.000.000",
                },
                {
                    "name": "hypersec-enrichment-ch-event-subschema-map.csv",
                    "version": "v001.000.000",
                },
                {
                    "name": "hypersec-enrichment-ch-fields-with-invalid-char.csv",
                    "version": "v001.000.000",
                },
                {
                    "name": "hypersec-enrichment-ch-remap-fields.csv",
                    "version": "v001.000.000",
                },
            ],
            "ip_config_receiver": [
                {
                    "name": "hypersec-receiver-event-category-map.csv",
                    "version": "v001.000.000",
                }
            ],
            "vector_enrichment_maxmind_geoip": [
                {"name": "GeoLite2-Country.mmdb", "version": "v001.000.000"},
                {"name": "GeoLite2-City.mmdb", "version": "v001.000.000"},
                {"name": "GeoLite2-ASN.mmdb", "version": "v001.000.000"},
            ],
            "ingestion_pipeline_globals": {
                "ingestion_data_dir": "/vector-data-dir",
                "ingestion_mtls_path": "/etc/vector_tls",
                "kafka_mtls_path": "/etc/vector_mtls",
                "kafka_brokers": "localhost:9092",
            },
            "ingestion_pipelines": [
                {
                    "name": "test_pipeline",
                    "stages": [
                        {
                            "name": "finalise",
                            "description": "Finalization stage",
                            "config": {
                                "kafka_source_topic": "test_topic",
                                "kafka_source_topic_suffix": "_land",
                                "kafka_sink_topic": "test_topic_load",
                                "kafka_consumer_group": "test-group",
                                "aws_account_id": "123456789012",
                                "container_config": {
                                    "min_replicas": 1,
                                    "max_replicas": 1,
                                    "target_memory_util_percentage": 125,
                                    "target_CPU_utilization_percentage": 125,
                                    "persistence_size": "1Gi",
                                    "container_ports": [8686, 9090],
                                },
                            },
                        },
                        {
                            "name": "load_ch",
                            "description": "ClickHouse loading stage",
                            "config": {
                                "kafka_source_topic": "test_topic",
                                "kafka_source_topic_suffix": "_load",
                                "kafka_consumer_group": "test-group-ch",
                                "aws_account_id": "123456789012",
                                "container_config": {
                                    "min_replicas": 1,
                                    "max_replicas": 1,
                                    "target_memory_util_percentage": 125,
                                    "target_CPU_utilization_percentage": 125,
                                    "persistence_size": "1Gi",
                                    "container_ports": [8686, 9090],
                                },
                            },
                        },
                        {
                            "name": "load_os",
                            "description": "OpenSearch loading stage",
                            "config": {
                                "kafka_source_topic": "test_topic",
                                "kafka_source_topic_suffix": "_load",
                                "kafka_consumer_group": "test-group-os",
                                "aws_account_id": "123456789012",
                                "container_config": {
                                    "min_replicas": 1,
                                    "max_replicas": 1,
                                    "target_memory_util_percentage": 125,
                                    "target_CPU_utilization_percentage": 125,
                                    "persistence_size": "1Gi",
                                    "container_ports": [8686, 9090],
                                },
                            },
                        },
                        {
                            "name": "load_s3",
                            "description": "S3 loading stage",
                            "config": {
                                "kafka_source_topic": "test_topic_load",
                                "kafka_source_topic_suffix": "",
                                "kafka_consumer_group": "test-group-s3",
                                "aws_account_id": "123456789012",
                                "container_config": {
                                    "min_replicas": 1,
                                    "max_replicas": 1,
                                    "target_memory_util_percentage": 125,
                                    "target_CPU_utilization_percentage": 125,
                                    "persistence_size": "1Gi",
                                    "container_ports": [8686, 9090],
                                },
                            },
                        },
                    ],
                }
            ],
        }

        # Create the package file
        package_path = os.path.join(temp_dir, "dfe_package.yaml")
        with open(package_path, "w") as f:
            yaml.dump(config_data, f)

        return config_data, package_path

    @staticmethod
    def create_test_dfe_targets(tmp_path_factory, config_data) -> tuple[dict, str]:
        """
        Creates a test dfe_targets.yaml file with minimal configuration needed for testing.

        Args:
            tmp_path_factory: pytest's tmp_path_factory fixture
            config_data: The configuration data from create_test_dfe_package

        Returns:
            tuple: (targets_dict, path_to_yaml_file)
        """
        targets_data = {
            "default_target": "integration",
            "targets": {
                "integration": {
                    "ch_host": "localhost",
                    "ch_port": 8123,
                    "ip_config_bucket_name": "test-vector-config-bucket",
                    "ip_config_bucket_region": "ap-southeast-2",
                    "ip_config_standard_enrichment_path": "standard_enrichment_files",
                    "ip_config_receiver_path": "standard_enrichment_files",
                    "ip_config_geo_ip_path": "geoip",
                    "ip_templates_path": "vector_templates",
                    "helm_template": "resources/pipeline_template/config_only.yaml",
                    "vector_config_mount_path": "/etc/vector",
                    "hunt_config_path": "dfecli/stable/hunt",
                    "hunt_rules_path": "dfecli/stable/rules",
                }
            },
        }

        # Create the targets file in the same directory as dfe_package.yaml
        targets_path = Path(config_data["global_settings"]["target_path"])
        os.makedirs(os.path.dirname(targets_path), exist_ok=True)

        with open(targets_path, "w") as f:
            yaml.dump(targets_data, f)

        return targets_data, str(targets_path)
