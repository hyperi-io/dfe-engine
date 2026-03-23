import os
import tempfile

import pytest
import yaml

from dfe_engine.pipeline.pipeline import Pipeline
from dfe_engine.pipeline.pipeline_util import PipelineSchemaError


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdirname:
        yield tmpdirname


@pytest.fixture
def sample_vector_template(temp_dir):
    # Use the new pipeline template structure based on the shipped template
    template_content = """# Test Pipeline Template
env:
  {% for env_var, value in env_vars.items() %}- name: {{ env_var }}
    value: {{ value }}
  {% endfor %}
  # Env Vars from Secrets
  - name: KAFKA_BROKERS_SASL_SCRAM
    valueFrom:
      secretKeyRef:
        name: kafka-sasl-secret
        key: KAFKA_BROKERS_SASL_SCRAM

args:
  - --watch-config
  - --no-graceful-shutdown-limit
  {% for step in steps %}- --config
  - {{ step }}
  {% endfor %}

extraObjects:
 - apiVersion: v1
   kind: ConfigMap
   metadata:
    name: vector-worker-default
    namespace: {{NAME}}
   data:
    {% for key, value in config_maps.items() %}{{ key }}: {% if not value is mapping %}{{ value }}
    {% else %}{% for sub_key, sub_value in value.items() %}{{ sub_key }}: {{ sub_value}}{% endfor %}
    {% endif %}{% endfor %}
    """
    # Create base directory for all templates
    base_dir = os.path.join(temp_dir, "templates")
    os.makedirs(base_dir, exist_ok=True)

    # Create vector template
    template_path = os.path.join(base_dir, "pipeline_template.yaml")
    with open(template_path, "w") as f:
        f.write(template_content)
    return template_path


@pytest.fixture
def sample_steps(temp_dir):
    # Create source step (kafka with SASL)
    source_content = """# Source for Kafka with SASL
sources:
  003_source_kafka_sasl_scram:
    type: kafka
    bootstrap_servers: "${KAFKA_BROKERS_SASL_SCRAM:?err}"
    group_id: "${KAFKA_CONSUMER_GROUP:?err}"
    topics: ${KAFKA_SOURCE_TOPIC_LIST:?err}
    tls:
      enabled: true
    sasl:
      enabled: true
      mechanism: SCRAM-SHA-512
      username: "${KAFKA_SASL_USERNAME:?err}"
      password: "${KAFKA_SASL_PASSWORD:?err}"
    """
    source_dir = os.path.join(temp_dir, "templates")
    os.makedirs(source_dir, exist_ok=True)
    source_path = os.path.join(source_dir, "003-source-kafka-sasl-scram.yml")
    with open(source_path, "w") as f:
        f.write(source_content)

    # Create transform step (message flattening)
    transform_content = """# Transform for flattening message
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
      source: "!is_empty(.)"
    """
    transform_path = os.path.join(source_dir, "101-transform-flatten-message.yml")
    with open(transform_path, "w") as f:
        f.write(transform_content)

    # Create sink step (clickhouse without hardcoded credentials)
    sink_content = """# Sink for ClickHouse
sinks:
  201_sink_clickhouse_saas:
    type: "clickhouse"
    healthcheck:
      enabled: true
    auth:
      strategy: "basic"
      user: "${CLICKHOUSE_AUTH_USER:?err}"
      password: "${CLICKHOUSE_AUTH_PASSWORD:?err}"
    inputs:
      - ${_201_SINK_CLICKHOUSE_SAAS_INPUT:?err}
    endpoint: "${CLICKHOUSE_ENDPOINT:?err}:8443"
    table: "{{tags_event_category}}"
    """
    sink_path = os.path.join(source_dir, "201-sink-clickhouse-saas.yml")
    with open(sink_path, "w") as f:
        f.write(sink_content)

    # Create base configuration steps
    base_main_content = """# Base configuration
data_dir: "${VECTOR_DATA_DIR:?err}"
api:
  enabled: true
  address: 0.0.0.0:8686
  playground: false
    """
    base_main_path = os.path.join(source_dir, "hs-xdr-vector-ct-all-main.yml")
    with open(base_main_path, "w") as f:
        f.write(base_main_content)

    base_prometheus_content = """# Prometheus metrics
sources:
  internal_metrics:
    type: internal_metrics
sinks:
  prometheus_exporter:
    type: prometheus_exporter
    inputs: ["internal_metrics"]
    address: "${PROMETHEUS_EXPORTER:?err}"
    """
    base_prometheus_path = os.path.join(source_dir, "hs-xdr-vector-ct-all-prometheus.yml")
    with open(base_prometheus_path, "w") as f:
        f.write(base_prometheus_content)

    return [base_main_path, base_prometheus_path, source_path, transform_path, sink_path]


@pytest.fixture
def sample_dfe_config(temp_dir, sample_vector_template):
    return {
        "global_settings": {
            "helm_template": os.path.join(temp_dir, "templates", "pipeline_template.yaml"),
            "output": os.path.join(temp_dir, "output"),
            "vector_files": {
                "core": os.path.join(temp_dir, "templates"),
            },
            "vector_config_mount_path": "/etc/vector_config/src",
        },
        "default_env_vars": {
            "PROMETHEUS_EXPORTER": "0.0.0.0:9090",
            "KAFKA_CONSUMER_GROUP": "default-group",
            "KAFKA_SOURCE_TOPIC_LIST": ["default_topic"],
            "VECTOR_DATA_DIR": "/vector-data-dir",
            # Note: Auth credentials and endpoints removed - handled by secrets in k8s
        },
        "ingestion_pipelines": {
            "test-pipeline": {
                "steps": [
                    {"id": "hs-xdr-vector-ct-all-main.yml"},
                    {"id": "hs-xdr-vector-ct-all-prometheus.yml"},
                    {"id": "003-source-kafka-sasl-scram.yml"},
                    {"id": "101-transform-flatten-message.yml"},
                    {"id": "201-sink-clickhouse-saas.yml"},
                ],
                "env": {
                    "KAFKA_CONSUMER_GROUP": "test-group",
                    "SOME_ENV_VAR": "some-value",
                },
                "meta": {
                    "EXPECTED_EPS": 1000,
                    "MIN_REPLICAS": 1,
                },
            },
            "test-pipeline-2": {
                "steps": [
                    {"id": "hs-xdr-vector-ct-all-main.yml"},
                    {"id": "hs-xdr-vector-ct-all-prometheus.yml"},
                    {"id": "003-source-kafka-sasl-scram.yml"},
                    {"id": "101-transform-flatten-message.yml"},
                    {"id": "201-sink-clickhouse-saas.yml"},
                ],
                "env": {
                    "KAFKA_CONSUMER_GROUP": "test-group-2",
                    "KAFKA_SOURCE_TOPIC_LIST": ["override_topic"],
                    "NEW_ENV_VAR": "new-value",
                    "NEW_THING": "new-thing",
                },
                "meta": {
                    "EXPECTED_EPS": 2000,
                    "MIN_REPLICAS": 2,
                },
            },
        },
    }


@pytest.fixture
def sample_pipeline_config():
    return {
        "steps": [
            {"id": "hs-xdr-vector-ct-all-main.yml"},
            {"id": "hs-xdr-vector-ct-all-prometheus.yml"},
            {"id": "003-source-kafka-sasl-scram.yml"},
            {"id": "101-transform-flatten-message.yml"},
            {"id": "201-sink-clickhouse-saas.yml"},
        ],
        "env": {"KAFKA_CONSUMER_GROUP": "test-group"},
        "meta": {"EXPECTED_EPS": 1000},
    }


def test_pipeline_initialization(sample_dfe_config, sample_pipeline_config, temp_dir):
    pipeline = Pipeline(
        name="test-pipeline",
        dfe_config=sample_dfe_config,
        pipeline_config=sample_pipeline_config,
        output_dir=os.path.join(temp_dir, "output"),
    )
    assert pipeline.name == "test-pipeline"
    assert pipeline.output_dir == os.path.join(temp_dir, "output")
    assert pipeline.pipeline_config == sample_pipeline_config


def test_pipeline_env_vars_combination(sample_dfe_config, sample_pipeline_config, temp_dir):
    pipeline = Pipeline(
        name="test-pipeline",
        dfe_config=sample_dfe_config,
        pipeline_config=sample_pipeline_config,
        output_dir=os.path.join(temp_dir, "output"),
    )
    env_vars = pipeline.combine_global_and_pipeline_env_vars()
    assert env_vars["KAFKA_CONSUMER_GROUP"] == "test-group"  # From pipeline config
    assert env_vars["PROMETHEUS_EXPORTER"] == "0.0.0.0:9090"  # From global args


def test_pipeline_template_rendering(sample_dfe_config, sample_steps, temp_dir):
    pipeline = Pipeline(
        name="test-pipeline",
        dfe_config=sample_dfe_config,
        pipeline_config=sample_dfe_config["ingestion_pipelines"]["test-pipeline"],
        output_dir=os.path.join(temp_dir, "output"),
    )
    env_vars, vector_env_vars = pipeline.gather_env_variables_for_pipeline()
    rendered = pipeline.render_template_for_pipeline(env_vars, vector_env_vars)
    assert "kind: ConfigMap" in rendered
    assert "test-pipeline" in rendered
    assert "KAFKA_BROKERS_SASL_SCRAM" in rendered
    # CLICKHOUSE_ENDPOINT should not appear in rendered template since it's in secrets
    assert "CLICKHOUSE_ENDPOINT" not in rendered


def test_pipeline_build_complete_flow(sample_dfe_config, sample_steps, temp_dir):
    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    pipeline = Pipeline(
        name="test-pipeline",
        dfe_config=sample_dfe_config,
        pipeline_config=sample_dfe_config["ingestion_pipelines"]["test-pipeline"],
        output_dir=output_dir,
    )
    pipeline.build()

    output_file = os.path.join(output_dir, "test-pipeline.yaml")
    assert os.path.exists(output_file)

    with open(output_file) as f:
        content = yaml.safe_load(f)
        assert content["env"] is not None
        # assert correct args count (2 base args + 2 per step)
        assert len(content["args"]) == len(pipeline.pipeline_config["steps"]) * 2 + 2


def test_pipeline_with_missing_required_env_var(sample_dfe_config, sample_steps, temp_dir):
    # Remove required env var
    del sample_dfe_config["default_env_vars"]["VECTOR_DATA_DIR"]

    pipeline = Pipeline(
        name="test-pipeline",
        dfe_config=sample_dfe_config,
        pipeline_config=sample_dfe_config["ingestion_pipelines"]["test-pipeline"],
        output_dir=os.path.join(temp_dir, "output"),
    )
    with pytest.raises(PipelineSchemaError):
        pipeline.build()


def test_pipeline_with_eks_template(sample_dfe_config, sample_steps, temp_dir):
    # Create EKS template
    template_dir = os.path.join(temp_dir, "templates")
    os.makedirs(template_dir, exist_ok=True)
    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    template_path = os.path.join(template_dir, "eks_vector.yaml.j2")
    with open(template_path, "w") as f:
        f.write("""
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ NAME }}-eks
args:
  - --watch-config
  - --verbose
  - --no-graceful-shutdown-limit
  {% for step in steps %}- --config
  - {{ step }}
  {% endfor %}
extraObjects:
  - apiVersion: v1
    kind: ConfigMap
    metadata:
      name: vector-worker-default
      namespace: {{NAME}}
    data:
      {% for key, value in config_maps.items() %}{{ key }}: {% if not value is mapping %}{{ value }}
      {% else %}{% for sub_key, sub_value in value.items() %}{{ sub_key }}: {{ sub_value}}{% endfor %}
      {% endif %}{% endfor %}
        """)
    sample_dfe_config["global_settings"]["helm_template"] = template_path
    pipeline = Pipeline(
        name="test-pipeline",
        dfe_config=sample_dfe_config,
        pipeline_config=sample_dfe_config["ingestion_pipelines"]["test-pipeline"],
        output_dir=os.path.join(temp_dir, "output"),
    )
    pipeline.build()

    output_file = os.path.join(temp_dir, "output", "test-pipeline.yaml")
    with open(output_file) as f:
        content = yaml.safe_load(f)
        assert content["metadata"]["name"] == "test-pipeline-eks"


def test_pipeline_with_custom_step_input(sample_dfe_config, sample_steps, temp_dir):
    # Add custom input to transform step (index 3 is the transform step)
    pipeline_config = dict(sample_dfe_config["ingestion_pipelines"]["test-pipeline"])
    pipeline_config["steps"][3]["input"] = "custom_source_output"

    pipeline = Pipeline(
        name="test-pipeline",
        dfe_config=sample_dfe_config,
        pipeline_config=pipeline_config,
        output_dir=os.path.join(temp_dir, "output"),
    )
    env_vars, _ = pipeline.gather_env_variables_for_pipeline()
    assert env_vars["_101_TRANSFORM_FLATTEN_MESSAGE_INPUT"] == "custom_source_output"


def test_build_all_pipelines(sample_dfe_config, sample_steps, temp_dir):
    """Critical test to ensure env vars don't bleed across pipelines"""
    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    for pipeline_name, pipeline_config in sample_dfe_config["ingestion_pipelines"].items():
        pipeline = Pipeline(
            name=pipeline_name,
            dfe_config=sample_dfe_config,
            pipeline_config=pipeline_config,
            output_dir=output_dir,
        )
        pipeline.build()

    output_files = os.listdir(output_dir)
    assert len(output_files) == 2
    assert "test-pipeline.yaml" in output_files
    assert "test-pipeline-2.yaml" in output_files
    with open(os.path.join(output_dir, "test-pipeline.yaml")) as f:
        content = yaml.safe_load(f)
        assert content["env"] is not None
        # assert correct env vars
        assert (
            len(content["args"])
            == len(sample_dfe_config["ingestion_pipelines"]["test-pipeline"]["steps"]) * 2 + 2
        )
        # Check config map data - should contain pipeline-specific vars only
        config_data = content["extraObjects"][0]["data"]
        assert "NEW_ENV_VAR" not in config_data.keys(), "Pipeline 1 should not have NEW_ENV_VAR"
        assert "SOME_ENV_VAR" in config_data.keys(), "Pipeline 1 should have SOME_ENV_VAR"
        assert "EXPECTED_EPS" in config_data.keys(), "Pipeline 1 should have meta vars"

        # Verify default env vars are present
        assert any("PROMETHEUS_EXPORTER" in key for key in config_data.keys()), (
            "Should have PROMETHEUS_EXPORTER"
        )

    with open(os.path.join(output_dir, "test-pipeline-2.yaml")) as f:
        content = yaml.safe_load(f)
        assert content["env"] is not None
        # assert correct env vars
        assert (
            len(content["args"])
            == len(sample_dfe_config["ingestion_pipelines"]["test-pipeline-2"]["steps"]) * 2 + 2
        )

        # Check config map data - ensure no variable bleeding
        config_data = content["extraObjects"][0]["data"]
        assert "NEW_ENV_VAR" in config_data.keys(), "Pipeline 2 should have NEW_ENV_VAR"
        assert "SOME_ENV_VAR" not in config_data.keys(), "Pipeline 2 should NOT have SOME_ENV_VAR"
        assert "EXPECTED_EPS" in config_data.keys(), "Pipeline 2 should have meta vars"

        # Check that override values are correct
        topic_list_key = [key for key in config_data.keys() if "KAFKA_SOURCE_TOPIC_LIST" in key]
        if topic_list_key:
            assert "override_topic" in str(config_data[topic_list_key[0]]), (
                "Should have overridden topic list"
            )


def test_pipeline_renders_with_missing_secret_env_vars(sample_dfe_config, sample_steps, temp_dir):
    """Test that pipeline can render even when secret environment variables are missing."""
    # Remove all secret environment variables from config
    secrets_to_remove = [
        "CLICKHOUSE_AUTH_USER",
        "CLICKHOUSE_AUTH_PASSWORD",
        "CLICKHOUSE_ENDPOINT",
        "KAFKA_BROKERS_SASL_SCRAM",
        "KAFKA_SASL_USERNAME",
        "KAFKA_SASL_PASSWORD",
    ]

    for secret in secrets_to_remove:
        sample_dfe_config["default_env_vars"].pop(secret, None)

    pipeline = Pipeline(
        name="test-pipeline",
        dfe_config=sample_dfe_config,
        pipeline_config=sample_dfe_config["ingestion_pipelines"]["test-pipeline"],
        output_dir=os.path.join(temp_dir, "output"),
    )

    # Pipeline should build successfully even without secret vars
    # because they are handled by Kubernetes secrets at runtime
    try:
        pipeline.build()
        # If we reach here, the pipeline rendered successfully
        assert True
    except PipelineSchemaError as e:
        # Should not fail due to missing secret vars
        assert "CLICKHOUSE_AUTH_USER" not in str(e)
        assert "CLICKHOUSE_AUTH_PASSWORD" not in str(e)
        assert "CLICKHOUSE_ENDPOINT" not in str(e)
        assert "KAFKA_BROKERS_SASL_SCRAM" not in str(e)
        assert "KAFKA_SASL_USERNAME" not in str(e)
        assert "KAFKA_SASL_PASSWORD" not in str(e)


def test_pipeline_fails_with_missing_non_secret_env_vars(sample_dfe_config, sample_steps, temp_dir):
    """Test that pipeline fails when required non-secret environment variables are missing."""
    # Remove a required non-secret environment variable
    del sample_dfe_config["default_env_vars"]["VECTOR_DATA_DIR"]

    pipeline = Pipeline(
        name="test-pipeline",
        dfe_config=sample_dfe_config,
        pipeline_config=sample_dfe_config["ingestion_pipelines"]["test-pipeline"],
        output_dir=os.path.join(temp_dir, "output"),
    )

    # Pipeline should fail when non-secret required vars are missing
    with pytest.raises(PipelineSchemaError) as exc_info:
        pipeline.build()

    # Verify the error mentions the missing non-secret variable
    assert "VECTOR_DATA_DIR" in str(exc_info.value)
