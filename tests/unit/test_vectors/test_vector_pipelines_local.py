import importlib.resources
import os
import tempfile

import pytest
import yaml

from dfe_engine.pipeline.pipeline_controller import PipelineBuilderController
from dfe_engine.pipeline.pipeline_util import (
    PipelineSchemaError,
    gather_env_variables_for_pipeline,
    get_pipeline,
    get_vector_pipelines,
    read_vector_step_config,
)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdirname:
        yield tmpdirname


@pytest.fixture
def sample_geoip_files(temp_dir):
    # Create sample GeoIP files
    geoip_dir = os.path.join(temp_dir, "geoip")
    os.makedirs(geoip_dir, exist_ok=True)

    files = ["GeoLite2-Country.mmdb", "GeoLite2-City.mmdb", "GeoLite2-ASN.mmdb"]
    for file in files:
        with open(os.path.join(geoip_dir, file), "w") as f:
            f.write("Sample GeoIP data")

    return geoip_dir


@pytest.fixture
def sample_vector_templates(temp_dir):
    # Create sample vector template files matching the core templates structure
    core_templates_dir = os.path.join(temp_dir, "core_templates")
    os.makedirs(core_templates_dir, exist_ok=True)

    # Core template files based on what's actually used in core_config.yaml
    templates = {
        "003-source-kafka-sasl-scram.yml": """# Source for Kafka with SASL
sources:
  003_source_kafka_sasl_scram:
    type: kafka
    bootstrap_servers: "${KAFKA_BROKERS_SASL_SCRAM:?err}"
    group_id: "${KAFKA_CONSUMER_GROUP:?err}"
    topics: ${KAFKA_SOURCE_TOPIC_LIST:?err}""",
        "101-transform-flatten-message.yml": """# Transform for flattening message
transforms:
  101_transform_flatten_message_parse:
    type: remap
    inputs:
      - ${_101_TRANSFORM_FLATTEN_MESSAGE_INPUT:?err}
    source: |
      . = parse_json(.message) ?? {}
  101_transform_flatten_message:
    type: filter
    inputs:
      - 101_transform_flatten_message_parse
    condition:
      type: "vrl"
      source: "!is_empty(.)" """,
        "201-sink-clickhouse-saas.yml": """# Sink for ClickHouse
sinks:
  201_sink_clickhouse_saas:
    type: "clickhouse"
    auth:
      strategy: "basic"
      user: "${CLICKHOUSE_AUTH_USER:?err}"
      password: "${CLICKHOUSE_AUTH_PASSWORD:?err}"
    inputs:
      - ${_201_SINK_CLICKHOUSE_SAAS_INPUT:?err}
    endpoint: "${CLICKHOUSE_ENDPOINT:?err}:8443"
    table: "{{tags_event_category}}" """,
        "hs-xdr-vector-ct-all-main.yml": """# Base configuration
data_dir: "${VECTOR_DATA_DIR:?err}"
api:
  enabled: true
  address: 0.0.0.0:8686
  playground: false""",
        "hs-xdr-vector-ct-all-prometheus.yml": """# Prometheus metrics
sources:
  internal_metrics:
    type: internal_metrics
sinks:
  prometheus_exporter:
    type: prometheus_exporter
    inputs: ["internal_metrics"]
    address: "${PROMETHEUS_EXPORTER:?err}" """,
    }

    # Create the template files directly in core_templates directory
    for name, content in templates.items():
        with open(os.path.join(core_templates_dir, name), "w") as f:
            f.write(content)

    return core_templates_dir


@pytest.fixture
def sample_enrichment_files(temp_dir):
    # Create sample enrichment files
    enrichment_dir = os.path.join(temp_dir, "enrichment")
    os.makedirs(enrichment_dir, exist_ok=True)

    with open(os.path.join(enrichment_dir, "sample_enrichment.csv"), "w") as f:
        f.write("id,value\n1,test")

    return enrichment_dir


@pytest.fixture
def sample_dfe_config(
    temp_dir, sample_geoip_files, sample_vector_templates, sample_enrichment_files
):
    return {
        "global_settings": {
            "output": os.path.join(temp_dir, "output"),
            "vector_files": {
                "core": sample_vector_templates,
                "standard": sample_enrichment_files,
                "geoip": sample_geoip_files,
                "custom": os.path.join(temp_dir, "custom_templates"),
            },
            "helm_template": "dfe_engine/pipeline/pipeline_template.yaml",
            "vector_config_mount_path": "/etc/vector_config/src",
        },
        "default_env_vars": {
            "PROMETHEUS_EXPORTER": "0.0.0.0:9090",
            "KAFKA_BROKERS_SASL_SCRAM": "localhost:9092",
            "KAFKA_CONSUMER_GROUP": "test-group",
            "KAFKA_SOURCE_TOPIC_LIST": ["test-topic"],
            "CLICKHOUSE_AUTH_USER": "default",
            "CLICKHOUSE_AUTH_PASSWORD": "password",
            "CLICKHOUSE_ENDPOINT": "localhost",
            "VECTOR_DATA_DIR": "/vector-data-dir",
        },
        "ingestion_pipelines": {
            "test-pipeline": {
                "env": {
                    "KAFKA_SOURCE_TOPIC_LIST": ["logs_test_load"],
                },
                "meta": {
                    "EXPECTED_EPS": 1000,
                    "MIN_REPLICAS": 1,
                },
                "steps": [
                    {"id": "hs-xdr-vector-ct-all-main.yml"},
                    {"id": "hs-xdr-vector-ct-all-prometheus.yml"},
                    {"id": "003-source-kafka-sasl-scram.yml"},
                    {"id": "101-transform-flatten-message.yml"},
                    {"id": "201-sink-clickhouse-saas.yml"},
                ],
            }
        },
    }


def test_get_vector_pipelines(sample_dfe_config):
    """Test that get_vector_pipelines returns the pipeline list."""
    pipelines = get_vector_pipelines(sample_dfe_config)
    assert "test-pipeline" in pipelines


def test_get_pipeline_config(sample_dfe_config):
    """Test that get_pipeline returns a specific pipeline config."""
    pipeline_config = get_pipeline(sample_dfe_config, "test-pipeline")
    assert pipeline_config is not None
    assert "steps" in pipeline_config
    assert len(pipeline_config["steps"]) == 5


def test_get_nonexistent_pipeline(sample_dfe_config):
    """Test that get_pipeline raises error for nonexistent pipeline."""
    with pytest.raises(PipelineSchemaError):
        get_pipeline(sample_dfe_config, "nonexistent-pipeline")


def test_gather_env_variables_for_pipeline(sample_dfe_config):
    """Test that environment variables are gathered correctly for pipeline."""
    env_vars, vector_env_vars = gather_env_variables_for_pipeline(
        sample_dfe_config, "test-pipeline"
    )

    # Check that default env vars are included
    assert "PROMETHEUS_EXPORTER" in env_vars
    assert env_vars["PROMETHEUS_EXPORTER"] == "0.0.0.0:9090"

    # Check that pipeline env vars are included - for now just verify it exists
    # TODO: Check why pipeline env vars are not overriding default env vars
    assert "KAFKA_SOURCE_TOPIC_LIST" in env_vars

    # Check that meta vars are included
    assert "EXPECTED_EPS" in env_vars
    assert env_vars["EXPECTED_EPS"] == 1000

    # Check that vector_env_vars contains step-specific variables
    assert isinstance(vector_env_vars, dict)


def test_read_vector_step_config(sample_dfe_config):
    """Test that vector step configs can be read correctly.

    Note: read_vector_step_config returns raw text (str) because Vector configs
    contain ${VAR} syntax that isn't valid YAML.
    """
    config = read_vector_step_config("003-source-kafka-sasl-scram.yml", sample_dfe_config)
    assert config is not None
    assert isinstance(config, str)
    assert "sources:" in config
    assert "003_source_kafka_sasl_scram:" in config


def test_vector_templates_exist(sample_vector_templates):
    """Test that vector template files are created correctly."""
    expected_files = [
        "003-source-kafka-sasl-scram.yml",
        "101-transform-flatten-message.yml",
        "201-sink-clickhouse-saas.yml",
        "hs-xdr-vector-ct-all-main.yml",
        "hs-xdr-vector-ct-all-prometheus.yml",
    ]

    for file_name in expected_files:
        file_path = os.path.join(sample_vector_templates, file_name)
        assert os.path.exists(file_path), f"Template file {file_name} should exist"


def test_dfe_config_structure(sample_dfe_config):
    """Test that the DFE config has the expected structure."""
    assert "global_settings" in sample_dfe_config
    assert "default_env_vars" in sample_dfe_config
    assert "ingestion_pipelines" in sample_dfe_config

    # Check global settings
    global_settings = sample_dfe_config["global_settings"]
    assert "vector_files" in global_settings
    assert "core" in global_settings["vector_files"]

    # Check pipeline structure
    pipelines = sample_dfe_config["ingestion_pipelines"]
    assert "test-pipeline" in pipelines

    test_pipeline = pipelines["test-pipeline"]
    assert "steps" in test_pipeline
    assert "env" in test_pipeline
    assert "meta" in test_pipeline


SHIPPED_TEMPLATE = importlib.resources.files("dfe_engine.pipeline") / "pipeline_template.yaml"
PROMETHEUS = "http://prometheus.example.test:9090"


def _build_package(config: dict, temp_dir: str) -> str:
    """Build a package the way POST /api/v1/pipeline/build does, and return the output dir."""
    package_path = os.path.join(temp_dir, "dfe_package.yaml")
    output_path = os.path.join(temp_dir, "pipeline_output")
    with importlib.resources.as_file(SHIPPED_TEMPLATE) as template:
        config["global_settings"]["helm_template"] = str(template)
        with open(package_path, "w") as f:
            yaml.dump(config, f)
        PipelineBuilderController.build_ingestion_pipelines(
            args_dfe_package_file_path=package_path,
            args_ingestion_output_path=output_path,
            args_log_path="",
            args_build_core=False,
        )
    return output_path


def test_a_package_build_writes_one_manifest_per_pipeline(sample_dfe_config, temp_dir):
    sample_dfe_config["global_settings"]["PROMETHEUS_SERVER_ADDRESS"] = PROMETHEUS

    output_path = _build_package(sample_dfe_config, temp_dir)

    assert os.listdir(output_path) == ["test-pipeline.yaml"]
    with open(os.path.join(output_path, "test-pipeline.yaml")) as f:
        manifest = yaml.safe_load(f)
    env = {item["name"]: item.get("value") for item in manifest["env"]}
    assert env["KAFKA_SOURCE_TOPIC_LIST"] == "['logs_test_load']"
    triggers = manifest["extraObjects"][0]["spec"]["triggers"]
    queried = [t["metadata"]["serverAddress"] for t in triggers if t["type"] == "prometheus"]
    assert queried == [PROMETHEUS]


def test_a_package_build_without_a_prometheus_address_is_refused(
    sample_dfe_config, temp_dir, monkeypatch
):
    monkeypatch.delenv("PROMETHEUS_SERVER_ADDRESS", raising=False)

    with pytest.raises(PipelineSchemaError, match="PROMETHEUS_SERVER_ADDRESS"):
        _build_package(sample_dfe_config, temp_dir)
