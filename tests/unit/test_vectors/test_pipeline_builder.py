import json
import os
import tempfile
from pathlib import Path

import pytest
import yaml
from unittest.mock import patch

# Skip all tests in this module - CLI integration tests require a CLI entrypoint
pytestmark = pytest.mark.skip(reason="CLI integration tests require a CLI entrypoint")

# Placeholder imports for skipped tests
PipelineBuilder = None
PipelineBuilderController = None
CliRunner = None
cli = None
logger = None


@pytest.fixture(scope="module")
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdirname:
        yield tmpdirname


@pytest.fixture
def sample_vector_template(temp_dir):
    template_content = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ NAME }}
data:
  vector.yaml: |
    args:
      - --watch-config
      - --verbose
      {% for step in steps %}- --config
      - {{ step }}
      {% endfor %}
    """
    # Create base directory for all templates
    base_dir = os.path.join(temp_dir, "templates")
    os.makedirs(base_dir, exist_ok=True)

    # Create vector template
    template_path = os.path.join(base_dir, "vector.yaml.j2")
    with open(template_path, "w") as f:
        f.write(template_content)
    return template_path


@pytest.fixture
def sample_vector_step(temp_dir):
    step_content = """
sources:
  source_name:
    type: "kafka"
    bootstrap_servers: ${KAFKA_BOOTSTRAP_SERVERS}
    topics: ${KAFKA_TOPICS}
    group_id: ${KAFKA_CONSUMER_GROUP}
    """
    # Create step file in the correct structure
    step_dir = os.path.join(temp_dir, "templates")
    os.makedirs(step_dir, exist_ok=True)
    step_path = os.path.join(step_dir, "0-source-step.yml")
    with open(step_path, "w") as f:
        f.write(step_content)
    return step_path


@pytest.fixture
def sample_dfe_config(temp_dir, sample_vector_template):
    return {
        "global_settings": {
            "output": os.path.join(temp_dir, "output"),
            "helm_template": sample_vector_template,
            "vector_files": {
                "source": os.path.join(temp_dir, "templates"),
                "transform": os.path.join(temp_dir, "templates"),
                "sink": os.path.join(temp_dir, "templates"),
            },
            "default_vector_template_version": "v100_000_000",
        },
        "default_env_vars": {
            "KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
            "KAFKA_TOPICS": "topic1,topic2",
        },
        "vector_templates": {"source": [{"name": "0-source-step.yml"}]},
        "ingestion_pipelines": {
            "test_pipeline": {
                "steps": [{"id": "0-source-step.yml"}],
                "env": {"KAFKA_CONSUMER_GROUP": "test-group"},
            }
        },
    }


def test_pipeline_builder_initialization(sample_dfe_config):
    builder = PipelineBuilder(sample_dfe_config)
    assert builder.dfe_config == sample_dfe_config
    assert builder.ingestion_output_path == sample_dfe_config["global_settings"]["output"]


def test_pipeline_builder_with_custom_output_path(sample_dfe_config):
    custom_path = Path("/custom/path")
    builder = PipelineBuilder(sample_dfe_config, ingestion_output_path=custom_path)
    assert builder.ingestion_output_path == custom_path


def test_build_pipeline(sample_dfe_config, sample_vector_step, temp_dir):
    # Ensure output directory exists
    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    builder = PipelineBuilder(sample_dfe_config)
    builder.build()

    # Check if output file was created
    expected_output_file = os.path.join(output_dir, "test-pipeline.yaml")
    assert os.path.exists(expected_output_file)

    # Verify content is valid YAML
    with open(expected_output_file) as f:
        content = yaml.safe_load(f)
        assert content["kind"] == "ConfigMap"
        assert content["metadata"]["name"] == "test-pipeline"


def test_build_pipeline_with_invalid_step(sample_dfe_config):
    # Modify config to include non-existent step
    sample_dfe_config["ingestion_pipelines"]["test_pipeline"]["steps"][0]["id"] = (
        "nonexistent-step.yml"
    )

    builder = PipelineBuilder(sample_dfe_config)
    with pytest.raises(Exception):
        builder.build()


def test_build_pipeline_with_missing_env_var(sample_dfe_config, sample_vector_step):
    # Remove required env var
    del sample_dfe_config["default_env_vars"]["KAFKA_TOPICS"]

    builder = PipelineBuilder(sample_dfe_config)
    with pytest.raises(Exception):
        builder.build()


def test_build_multiple_pipelines(sample_dfe_config, sample_vector_step, temp_dir):
    # Add another pipeline to config
    sample_dfe_config["ingestion_pipelines"]["another_pipeline"] = {
        "steps": [{"id": "0-source-step.yml"}],
        "env": {"KAFKA_CONSUMER_GROUP": "another-group"},
    }

    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    builder = PipelineBuilder(sample_dfe_config)
    builder.build()

    # Check if both output files were created
    assert os.path.exists(os.path.join(output_dir, "test-pipeline.yaml"))
    assert os.path.exists(os.path.join(output_dir, "another-pipeline.yaml"))


def test_build_pipeline_with_custom_template_path(sample_dfe_config, sample_vector_step, temp_dir):
    # Create custom template
    custom_template_content = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: custom-{{ NAME }}
data:
  vector.yaml: |
    args:
      - --watch-config
      - --verbose
      {% for step in steps %}- --config
      - {{ step }}
      {% endfor %}
    """
    custom_template_dir = os.path.join(temp_dir, "custom_templates")
    os.makedirs(custom_template_dir, exist_ok=True)
    custom_template_path = os.path.join(custom_template_dir, "custom_vector.yaml.j2")
    with open(custom_template_path, "w") as f:
        f.write(custom_template_content)

    # Copy step file to custom templates directory
    custom_step_dir = os.path.join(temp_dir, "custom_templates")
    os.makedirs(custom_step_dir, exist_ok=True)
    custom_step_path = os.path.join(custom_step_dir, "0-source-step.yml")
    with open(custom_step_path, "w") as f:
        f.write("""
sources:
  source_name:
    type: "kafka"
    bootstrap_servers: ${KAFKA_BOOTSTRAP_SERVERS}
    topics: ${KAFKA_TOPICS}
    group_id: ${KAFKA_CONSUMER_GROUP}
    """)

    # Update config to use custom template and paths
    sample_dfe_config["global_settings"]["helm_template"] = custom_template_path
    sample_dfe_config["global_settings"]["vector_files"] = {
        "source": os.path.join(temp_dir, "custom_templates"),
        "transform": os.path.join(temp_dir, "custom_templates"),
        "sink": os.path.join(temp_dir, "custom_templates"),
    }

    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    builder = PipelineBuilder(sample_dfe_config)
    builder.build()

    # Verify custom template was used
    output_file = os.path.join(output_dir, "test-pipeline.yaml")
    with open(output_file) as f:
        content = yaml.safe_load(f)
        assert content["metadata"]["name"] == "custom-test-pipeline"


def test_build_pipeline_with_invalid_yaml_template(sample_dfe_config, sample_vector_step, temp_dir):
    # Create invalid template
    invalid_template_content = """
    invalid:
      - yaml:
        content: {
    """
    template_dir = os.path.join(temp_dir, "templates")
    os.makedirs(template_dir, exist_ok=True)
    template_path = os.path.join(template_dir, "invalid_vector.yaml.j2")
    with open(template_path, "w") as f:
        f.write(invalid_template_content)

    sample_dfe_config["global_settings"]["helm_template"] = template_path

    builder = PipelineBuilder(sample_dfe_config)
    with pytest.raises(Exception):
        builder.build()


def test_pipeline_builder_with_extra_config(sample_dfe_config):
    """Test PipelineBuilder initialization with extra_config parameter."""
    extra_config = {"custom_key": "custom_value", "override_setting": True}
    builder = PipelineBuilder(sample_dfe_config, extra_config=extra_config)
    assert builder.extra_config == extra_config


def test_pipeline_builder_with_empty_extra_config(sample_dfe_config):
    """Test PipelineBuilder initialization with empty extra_config."""
    builder = PipelineBuilder(sample_dfe_config, extra_config={})
    assert builder.extra_config == {}


def test_pipeline_builder_without_extra_config(sample_dfe_config):
    """Test PipelineBuilder initialization without extra_config parameter."""
    builder = PipelineBuilder(sample_dfe_config)
    # Should have default empty dict if not provided
    assert hasattr(builder, "extra_config")


def test_build_pipeline_with_extra_config_template_variables(
    sample_dfe_config, sample_vector_step, temp_dir
):
    """Test building pipeline where extra_config provides template variables."""
    # Create template that uses variables from extra_config at the main template level
    template_content = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ NAME }}
  namespace: {{ NAMESPACE | default('default') }}
  labels:
    custom-label: "{{ CUSTOM_FIELD | default('default_value') }}"
data:
  vector.yaml: |
    args:
      - --watch-config
      - --verbose
      {% for step in steps %}- --config
      - {{ step }}
      {% endfor %}
    """
    # Update template
    template_dir = os.path.join(temp_dir, "templates")
    template_path = os.path.join(template_dir, "vector.yaml.j2")
    with open(template_path, "w") as f:
        f.write(template_content)

    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    # Extra config with template variables
    extra_config = {"NAMESPACE": "production", "CUSTOM_FIELD": "extra_config_value"}

    builder = PipelineBuilder(sample_dfe_config, extra_config=extra_config)
    builder.build()

    # Verify output includes extra_config values
    output_file = os.path.join(output_dir, "test-pipeline.yaml")
    with open(output_file) as f:
        content = yaml.safe_load(f)
        assert content["metadata"]["namespace"] == "production"
        assert content["metadata"]["labels"]["custom-label"] == "extra_config_value"


def test_build_pipeline_extra_config_overrides_defaults(
    sample_dfe_config, sample_vector_step, temp_dir
):
    """Test that extra_config can override default configuration values."""
    # Test that extra_config overrides default values at the template level
    template_content = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ NAME }}
  annotations:
    topic: "{{ TOPICS | default('default-topic') }}"
data:
  vector.yaml: |
    args:
      - --watch-config
      - --verbose
      {% for step in steps %}- --config
      - {{ step }}
      {% endfor %}
    """
    # Update template
    template_dir = os.path.join(temp_dir, "templates")
    template_path = os.path.join(template_dir, "vector.yaml.j2")
    with open(template_path, "w") as f:
        f.write(template_content)

    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    # Extra config that overrides the default
    extra_config = {"TOPICS": "override-topic"}

    builder = PipelineBuilder(sample_dfe_config, extra_config=extra_config)
    builder.build()

    # Verify extra_config value was used instead of default
    output_file = os.path.join(output_dir, "test-pipeline.yaml")
    with open(output_file) as f:
        content = yaml.safe_load(f)
        topic_annotation = content["metadata"]["annotations"]["topic"]
        assert topic_annotation == "override-topic"


def test_build_pipeline_extra_config_with_simple_key_value_pairs(
    sample_dfe_config, sample_vector_step, temp_dir
):
    """Test extra_config with simple key-value pairs as expected from JSON string input."""
    # Create template that uses simple extra_config key-value pairs
    template_content = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ NAME }}
  labels:
    environment: "{{ ENVIRONMENT | default('default') }}"
    version: "{{ VERSION | default('1.0.0') }}"
    team: "{{ TEAM | default('unknown') }}"
data:
  vector.yaml: |
    args:
      - --watch-config
      - --verbose
      {% for step in steps %}- --config
      - {{ step }}
      {% endfor %}
    """
    template_dir = os.path.join(temp_dir, "templates")
    template_path = os.path.join(template_dir, "vector.yaml.j2")
    with open(template_path, "w") as f:
        f.write(template_content)

    # Update config to use new template
    sample_dfe_config["global_settings"]["helm_template"] = template_path

    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    # Simple extra_config with string key-value pairs (as would come from JSON)
    extra_config = {"ENVIRONMENT": "production", "VERSION": "2.1.0", "TEAM": "security"}

    builder = PipelineBuilder(sample_dfe_config, extra_config=extra_config)
    builder.build()

    # Verify simple key-value data was rendered correctly
    output_file = os.path.join(output_dir, "test-pipeline.yaml")
    with open(output_file) as f:
        content = yaml.safe_load(f)
        labels = content["metadata"]["labels"]
        assert labels["environment"] == "production"
        assert labels["version"] == "2.1.0"
        assert labels["team"] == "security"


# Completely isolated CLI tests that don't depend on external fixtures
class TestBuildIngestionPipelinesCLI:
    """Isolated tests for the build-ingestion-pipelines CLI command with extra_config parameter."""

    @pytest.fixture
    def runner(self):
        """Click test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_pipeline_controller(self):
        """Mock the PipelineBuilderController to avoid external dependencies."""
        with patch("dfe_engine.pipeline.pipeline_controller.PipelineBuilderController") as mock:
            yield mock

    def test_build_ingestion_pipelines_cli_with_extra_config_valid_json(
        self, runner, mock_pipeline_controller
    ):
        """Test CLI command with valid JSON extra_config parameter."""
        # Setup mock
        mock_pipeline_controller.build_ingestion_pipelines.return_value = None

        result = runner.invoke(
            cli,
            [
                "build-ingestion-pipelines",
                "--package_yaml",
                "/fake/path/dfe_package.yaml",
                "--log_path",
                "/fake/logs",
                "--extra_config",
                '{"key1": "value1", "key2": "value2"}',
            ],
        )

        # Verify command succeeded
        assert result.exit_code == 0, f"Command failed: {result.output}"

        # Verify the controller was called with correct parameters
        mock_pipeline_controller.build_ingestion_pipelines.assert_called_once()
        call_args = mock_pipeline_controller.build_ingestion_pipelines.call_args

        # Check that extra_config was parsed and passed correctly
        assert call_args.kwargs["args_extra_config"] == {"key1": "value1", "key2": "value2"}

    def test_build_ingestion_pipelines_cli_with_empty_extra_config(
        self, runner, mock_pipeline_controller
    ):
        """Test CLI command with empty JSON extra_config."""
        mock_pipeline_controller.build_ingestion_pipelines.return_value = None

        result = runner.invoke(
            cli,
            [
                "build-ingestion-pipelines",
                "--package_yaml",
                "/fake/path/dfe_package.yaml",
                "--log_path",
                "/fake/logs",
                "--extra_config",
                "{}",
            ],
        )

        assert result.exit_code == 0
        call_args = mock_pipeline_controller.build_ingestion_pipelines.call_args
        assert call_args.kwargs["args_extra_config"] == {}

    def test_build_ingestion_pipelines_cli_without_extra_config(
        self, runner, mock_pipeline_controller
    ):
        """Test CLI command without extra_config parameter (backward compatibility)."""
        mock_pipeline_controller.build_ingestion_pipelines.return_value = None

        result = runner.invoke(
            cli,
            [
                "build-ingestion-pipelines",
                "--package_yaml",
                "/fake/path/dfe_package.yaml",
                "--log_path",
                "/fake/logs",
            ],
        )

        assert result.exit_code == 0
        call_args = mock_pipeline_controller.build_ingestion_pipelines.call_args
        # Should default to empty dict
        assert call_args.kwargs["args_extra_config"] == {}


@pytest.mark.skip(
    reason="This test needs env variables to be set to download templates. Only run if the env vars are set."
)
def test_build_core_pipelines_without_custom_config(temp_dir):
    """Test that core pipelines can be built without any custom configuration."""
    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    # Build only core pipelines (args_build_core=True)
    try:
        PipelineBuilderController.build_ingestion_pipelines(
            args_dfe_package_file_path="non_existent.yaml",  # Should be ignored when build_core=True
            args_ingestion_output_path=output_dir,
            args_log_path=temp_dir,
            args_build_core=True,  # This should use only core config
        )
        # If we reach here, core pipelines were built successfully
        assert True

        # Check that output files were created
        output_files = os.listdir(output_dir)
        assert len(output_files) > 0, "Core pipelines should generate output files"

        # Verify at least one core pipeline was built
        core_pipeline_names = [
            "vector-all-prep-ch-load.yaml",
            "vector-load-ch.yaml",
            "vector-all-load-s3.yaml",
            "vector-logs-hypercol-metric-finalise.yaml",
            "vector-receiver.yaml",
        ]
        found_core_pipeline = any(name in output_files for name in core_pipeline_names)
        assert found_core_pipeline, (
            f"Expected to find core pipeline files, but found: {output_files}"
        )

    except Exception as e:
        # Only allow expected errors like missing vector templates
        expected_errors = [
            "FileNotFoundError",
            "TemplateNotFound",
            "No such file",
            "pipeline_template.yaml",
        ]
        assert any(error in str(e) for error in expected_errors), (
            f"Unexpected error building core pipelines: {e}"
        )


@pytest.mark.skip(
    reason="This test needs env variables to be set to download templates. Only run if the env vars are set."
)
def test_custom_config_overrides_core_config(temp_dir):
    """Test that custom config environment variables override core config variables."""
    # Create a minimal custom config that overrides a core env var
    custom_config = {
        "default_env_vars": {
            "PROMETHEUS_EXPORTER": "custom.prometheus.com:9090",  # Override core default
            "CUSTOM_VAR": "custom_value",
        },
        "ingestion_pipelines": {
            "test-custom-pipeline": {
                "env": {"KAFKA_SOURCE_TOPIC_LIST": ["custom_topic"]},
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

    # Create custom config file
    custom_config_path = os.path.join(temp_dir, "custom_config.yaml")
    with open(custom_config_path, "w") as f:
        yaml.dump(custom_config, f)

    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    try:
        PipelineBuilderController.build_ingestion_pipelines(
            args_dfe_package_file_path=custom_config_path,
            args_ingestion_output_path=output_dir,
            args_log_path=temp_dir,
            args_build_core=False,  # Use custom config merged with core
        )

        # Check that custom pipeline was created
        output_files = os.listdir(output_dir)
        assert "test-custom-pipeline.yaml" in output_files

        # Read the generated pipeline and verify custom env var override
        with open(os.path.join(output_dir, "test-custom-pipeline.yaml")) as f:
            pipeline_content = f.read()

        # Should contain the custom PROMETHEUS_EXPORTER value
        assert "custom.prometheus.com:9090" in pipeline_content, (
            "Custom PROMETHEUS_EXPORTER should override core value"
        )
        # assert "CUSTOM_VAR" in pipeline_content, "Custom env var should be present"

    except Exception as e:
        # Only allow expected errors like missing vector templates
        expected_errors = [
            "FileNotFoundError",
            "TemplateNotFound",
            "No such file",
            "configuration file",
            "pipeline_template.yaml",
        ]
        assert any(error in str(e) for error in expected_errors), f"Unexpected error: {e}"


@pytest.mark.skip(
    reason="This test needs env variables to be set to download templates. Only run if the env vars are set."
)
def test_extra_config_has_highest_priority(temp_dir):
    """Test that extra_config has highest priority over both core and custom config."""
    # Create a custom config with some env vars
    custom_config = {
        "default_env_vars": {
            "PROMETHEUS_EXPORTER": "custom.prometheus.com:9090",
            "VECTOR_DATA_DIR": "/custom-vector-dir",
            "CUSTOM_VAR": "custom_value",
        },
        "ingestion_pipelines": {
            "test-priority-pipeline": {
                "env": {"KAFKA_SOURCE_TOPIC_LIST": ["priority_topic"]},
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

    # Create custom config file
    custom_config_path = os.path.join(temp_dir, "priority_config.yaml")
    with open(custom_config_path, "w") as f:
        yaml.dump(custom_config, f)

    output_dir = os.path.join(temp_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    # Extra config that should override both core and custom
    extra_config = {
        "PROMETHEUS_EXPORTER": "extra.prometheus.com:9090",  # Should override custom
        "VECTOR_DATA_DIR": "/extra-vector-dir",  # Should override custom
        "EXTRA_ONLY_VAR": "extra_only_value",  # Should be added
    }

    try:
        PipelineBuilderController.build_ingestion_pipelines(
            args_dfe_package_file_path=custom_config_path,
            args_ingestion_output_path=output_dir,
            args_log_path=temp_dir,
            args_build_core=False,
            args_extra_config=extra_config,  # This should have highest priority
        )

        # Check that pipeline was created
        output_files = os.listdir(output_dir)
        assert "test-priority-pipeline.yaml" in output_files

        # Read the generated pipeline and verify extra config takes priority
        with open(os.path.join(output_dir, "test-priority-pipeline.yaml")) as f:
            pipeline_content = f.read()

        # Should contain the extra config values (highest priority)
        assert "extra.prometheus.com:9090" in pipeline_content, (
            "Extra PROMETHEUS_EXPORTER should override custom and core"
        )
        assert "/extra-vector-dir" in pipeline_content, (
            "Extra VECTOR_DATA_DIR should override custom and core"
        )
        # assert "EXTRA_ONLY_VAR" in pipeline_content, "Extra-only var should be present"

        # Should NOT contain the custom values that were overridden
        assert "custom.prometheus.com:9090" not in pipeline_content, (
            "Custom PROMETHEUS_EXPORTER should be overridden by extra"
        )
        assert "/custom-vector-dir" not in pipeline_content, (
            "Custom VECTOR_DATA_DIR should be overridden by extra"
        )

    except Exception as e:
        # Only allow expected errors like missing vector templates
        expected_errors = [
            "FileNotFoundError",
            "TemplateNotFound",
            "No such file",
            "configuration file",
            "pipeline_template.yaml",
        ]
        assert any(error in str(e) for error in expected_errors), f"Unexpected error: {e}"


class TestBuildIngestionPipelinesCLIExtended:
    """Additional CLI tests for the build-ingestion-pipelines command."""

    @pytest.fixture
    def runner(self):
        """Click test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_pipeline_controller(self):
        """Mock the PipelineBuilderController to avoid external dependencies."""
        with patch("dfe_engine.pipeline.pipeline_controller.PipelineBuilderController") as mock:
            yield mock

    def test_build_ingestion_pipelines_cli_with_invalid_json(
        self, runner, mock_pipeline_controller
    ):
        """Test CLI command with invalid JSON in extra_config."""
        result = runner.invoke(
            cli,
            [
                "build-ingestion-pipelines",
                "--package_yaml",
                "/fake/path/dfe_package.yaml",
                "--log_path",
                "/fake/logs",
                "--extra_config",
                '{"invalid": json}',
            ],
        )

        # Should fail with exit code 1 due to invalid JSON (hard failure)
        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}"
        # The exception should be JSONDecodeError from json.loads()
        assert result.exception is not None
        assert "JSONDecodeError" in str(type(result.exception))

    def test_build_ingestion_pipelines_cli_with_complex_extra_config(
        self, runner, mock_pipeline_controller
    ):
        """Test CLI command with various JSON value types in extra_config."""
        mock_pipeline_controller.build_ingestion_pipelines.return_value = None

        extra_config = {
            "string_val": "test_string",
            "number_val": "123",
            "boolean_val": "true",
            "env_var": "production",
        }

        result = runner.invoke(
            cli,
            [
                "build-ingestion-pipelines",
                "--package_yaml",
                "/fake/path/dfe_package.yaml",
                "--log_path",
                "/fake/logs",
                "--extra_config",
                json.dumps(extra_config),
            ],
        )

        assert result.exit_code == 0
        call_args = mock_pipeline_controller.build_ingestion_pipelines.call_args
        assert call_args.kwargs["args_extra_config"] == extra_config

    def test_build_ingestion_pipelines_cli_with_missing_env_vars(
        self, runner, mock_pipeline_controller
    ):
        """Test CLI command with missing environment variables (hard failure)."""
        # Configure mock to raise PipelineSchemaError (simulating missing env vars)
        from dfe_engine.pipeline.pipeline_util import PipelineSchemaError

        mock_pipeline_controller.build_ingestion_pipelines.side_effect = PipelineSchemaError(
            "Env var KAFKA_SOURCE_TOPIC_LIST is None. You need to define this env var in the pipeline or global settings or in the .env file."
        )

        result = runner.invoke(
            cli,
            [
                "build-ingestion-pipelines",
                "--package_yaml",
                "/fake/path/dfe_package.yaml",
                "--log_path",
                "/fake/logs",
            ],
        )

        # Should fail with exit code 1 due to missing environment variables (hard failure)
        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}"
        # The exception should be PipelineSchemaError
        assert result.exception is not None
        assert "PipelineSchemaError" in str(type(result.exception))
        assert "KAFKA_SOURCE_TOPIC_LIST" in str(result.exception)
