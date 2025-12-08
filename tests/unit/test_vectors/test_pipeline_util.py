import pytest
import yaml
from dfe_engine.pipeline.pipeline_util import (
    VectorStepType,
    find_vector_type,
    get_vector_step_type,
    read_vector_step_config,
    get_required_env_vars,
    get_vector_pipelines,
    get_pipeline,
    vector_step_name_to_input,
    vector_step_name_to_output,
    gather_env_variables_for_pipeline,
    PipelineSchemaError,
)


# Test fixtures
@pytest.fixture
def setup_test_files(tmp_path):
    # Create directory structure
    sources_dir = tmp_path / "sources"
    transforms_dir = tmp_path / "transforms"
    sinks_dir = tmp_path / "sinks"

    # Create source step directories and files
    sources_dir = sources_dir
    sources_dir.parent.mkdir(parents=True, exist_ok=True)
    sources_dir.mkdir(exist_ok=True)
    source_config = {
        "sources": {
            "test_source": {
                "type": "file",
                "path": "${SOURCE_PATH}",
                "compression": "${COMPRESSION:-none}",
                "required_var": "${REQUIRED_VAR:?err}",
            }
        }
    }
    with open(sources_dir / "0-source-step.yml", "w") as f:
        yaml.dump(source_config, f)

    # Create transform step directories and files
    transforms_dir.parent.mkdir(parents=True, exist_ok=True)
    transforms_dir.mkdir(exist_ok=True)
    transform_config = {
        "transforms": {
            "test_transform": {
                "type": "remap",
                "inputs": ["${_1_TRANSFORM_STEP_INPUT}"],
                "source": "${TRANSFORM_SOURCE:-default}",
            }
        }
    }
    with open(transforms_dir / "1-transform-step.yml", "w") as f:
        yaml.dump(transform_config, f)

    # Create sink step directories and files
    sinks_dir.parent.mkdir(parents=True, exist_ok=True)
    sinks_dir.mkdir(exist_ok=True)
    sink_config = {
        "sinks": {
            "test_sink": {
                "type": "clickhouse",
                "inputs": ["${_2_SINK_STEP_INPUT}"],
                "endpoint": "${SINK_ENDPOINT:?err}",
            }
        }
    }
    with open(sinks_dir / "2-sink-step.yml", "w") as f:
        yaml.dump(sink_config, f)

    return tmp_path


@pytest.fixture
def sample_package_config(setup_test_files):
    return {
        "global_settings": {
            "default_vector_template_version": "v1_0_0",
            "vector_files": {
                "sources": str(setup_test_files / "sources"),
                "transforms": str(setup_test_files / "transforms"),
                "sinks": str(setup_test_files / "sinks"),
            },
        },
        "ingestion_pipelines": {
            "test-pipeline": {
                "steps": [
                    {"id": "0-source-step.yml"},
                    {"id": "1-transform-step.yml", "input": "custom_input"},
                    {"id": "2-sink-step.yml"},
                ],
                "env": {"CUSTOM_VAR": "custom_value"},
            }
        },
        "default_env_vars": {"GLOBAL_VAR": "global_value"},
    }


# Test VectorStepType and find_vector_type
def test_vector_step_type():
    assert VectorStepType.SOURCE.value == "0"
    assert VectorStepType.TRANSFORM.value == "1"
    assert VectorStepType.SINK.value == "2"
    assert VectorStepType.BASE.value == "hs"
    assert VectorStepType.HYPERSEC.value == "hypersec"


def test_find_vector_type():
    assert find_vector_type("0-source-step.yml") == VectorStepType.SOURCE
    assert find_vector_type("1-transform-step.yml") == VectorStepType.TRANSFORM
    assert find_vector_type("2-sink-step.yml") == VectorStepType.SINK
    assert find_vector_type("hs-base-step.yml") == VectorStepType.BASE
    assert find_vector_type("hypersec-step.yml") == VectorStepType.HYPERSEC
    assert find_vector_type("invalid-step.yml") is None


# Test vector step version and type retrieval
def test_get_vector_step_type(sample_package_config):
    step_type = get_vector_step_type(
        "0-source-step.yml", sample_package_config
    )
    assert step_type == "sources"

    step_type = get_vector_step_type(
        "1-transform-step.yml", sample_package_config
    )
    assert step_type == "transforms"


def test_get_vector_step_type_not_found(sample_package_config):
    with pytest.raises(PipelineSchemaError) as exc:
        get_vector_step_type("nonexistent-step.yml", sample_package_config)
    assert "not found. Make sure your template is located in either" in str(exc.value)


# Test vector step config reading
def test_read_vector_step_config(sample_package_config):
    config = read_vector_step_config("0-source-step.yml", sample_package_config)
    assert isinstance(config, dict)
    assert "sources" in config


def test_read_vector_step_config_invalid_type(sample_package_config):
    sample_package_config["global_settings"]["vector_files"] = {}
    with pytest.raises(PipelineSchemaError) as exc:
        read_vector_step_config("0-source-step.yml", sample_package_config)
    assert "not found. Make sure your template is located in either" in str(exc.value)


# Test environment variables extraction
def test_get_required_env_vars(sample_package_config):
    env_vars = get_required_env_vars("0-source-step.yml", sample_package_config)
    assert "SOURCE_PATH" in env_vars
    assert "COMPRESSION" in env_vars
    assert "REQUIRED_VAR" in env_vars
    assert env_vars["COMPRESSION"] == "none"
    assert env_vars["REQUIRED_VAR"] is None


# Test pipeline retrieval
def test_get_vector_pipelines(sample_package_config):
    pipelines = get_vector_pipelines(sample_package_config)
    assert "test-pipeline" in pipelines


def test_get_pipeline(sample_package_config):
    pipeline = get_pipeline(sample_package_config, "test-pipeline")
    assert len(pipeline["steps"]) == 3
    assert pipeline["steps"][0]["id"] == "0-source-step.yml"


def test_get_pipeline_not_found(sample_package_config):
    with pytest.raises(PipelineSchemaError) as exc:
        get_pipeline(sample_package_config, "nonexistent-pipeline")
    assert "not found in package config" in str(exc.value)


# Test vector step name conversions
def test_vector_step_name_to_input():
    assert vector_step_name_to_input("test-step.yml") == "_TEST_STEP_INPUT"
    assert vector_step_name_to_input("test_step") == "_TEST_STEP_INPUT"
    assert vector_step_name_to_input("test-step-name.yml") == "_TEST_STEP_NAME_INPUT"


def test_vector_step_name_to_output():
    assert vector_step_name_to_output("test-step.yml") == "test_step"
    assert vector_step_name_to_output("TEST_STEP.yml") == "test_step"
    assert vector_step_name_to_output("test-step-name.yml") == "test_step_name"


# Test environment variables gathering
def test_gather_env_variables_for_pipeline(sample_package_config):
    env_vars, _ = gather_env_variables_for_pipeline(sample_package_config, "test-pipeline")
    assert "GLOBAL_VAR" in env_vars
    assert "CUSTOM_VAR" in env_vars
    assert "_1_TRANSFORM_STEP_INPUT" in env_vars
    assert env_vars["GLOBAL_VAR"] == "global_value"
    assert env_vars["CUSTOM_VAR"] == "custom_value"
    assert env_vars["_1_TRANSFORM_STEP_INPUT"] == "custom_input"


# Edge cases and error handling
def test_empty_package_config():
    empty_config = {}
    with pytest.raises(PipelineSchemaError):
        get_vector_step_type("step.yml", empty_config)


def test_missing_vector_templates():
    config = {
        "global_settings": {
            "vector_files": {"test": "/path/to/test"}
        }
    }
    with pytest.raises(PipelineSchemaError):
        get_vector_step_type("step.yml", config)


def test_none_vector_templates():
    config = {
        "vector_templates": None,
        "global_settings": {
            "vector_files": {"test": "/path/to/test"}
        },
    }
    with pytest.raises(PipelineSchemaError):
        get_vector_step_type("step.yml", config)


def test_invalid_vector_step_names():
    assert vector_step_name_to_input("") == "__INPUT"
    assert vector_step_name_to_output("") == ""
    assert find_vector_type("") is None


def test_complex_env_var_patterns(setup_test_files):
    # Create a test file with complex environment variables
    test_dir = setup_test_files / "test"
    test_dir.mkdir(parents=True, exist_ok=True)

    # Create version directory structure
    test_dir.parent.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(exist_ok=True)

    test_config = {
        "complex_vars": {
            "var1": "${VAR1?:err}",
            "var2": "${VAR2:-default:with:colons}",
            "var3": "${VAR3:-}",
            "var4": "${4INVALID}",  # Should be ignored as it starts with a number
            "var5": "${VAR5:-default-with-hyphens}",
        }
    }
    with open(test_dir / "test-step.yml", "w") as f:
        yaml.dump(test_config, f)

    package_config = {
        "vector_templates": {"test": [{"name": "test-step.yml", "version": "v1_0_0"}]},
        "global_settings": {
            "vector_files": {"test": str(test_dir)}
        },
    }
    env_vars = get_required_env_vars("test-step.yml", package_config)
    assert "VAR1" in env_vars
    assert "VAR2" in env_vars
    assert "VAR3" in env_vars
    assert "4INVALID" not in env_vars
    assert env_vars["VAR2"] == "default:with:colons"
    assert env_vars["VAR5"] == "default-with-hyphens"


# Test merge_configs function
def test_merge_configs():
    from dfe_engine.pipeline.pipeline_util import merge_configs
    
    # Test basic merging
    default = {"a": 1, "b": {"c": 2}}
    override = {"b": {"d": 3}, "e": 4}
    result = merge_configs(default, override)
    
    assert result["a"] == 1
    assert result["b"]["c"] == 2
    assert result["b"]["d"] == 3
    assert result["e"] == 4


def test_merge_configs_list_handling():
    from dfe_engine.pipeline.pipeline_util import merge_configs
    
    # Test list merging and deduplication
    default = {"items": [1, 2, 3]}
    override = {"items": [3, 4, 5]}
    result = merge_configs(default, override)
    
    assert result["items"] == [1, 2, 3, 4, 5]


def test_merge_configs_scalar_override():
    from dfe_engine.pipeline.pipeline_util import merge_configs
    
    # Test scalar value override
    default = {"value": "old", "keep": "same"}
    override = {"value": "new"}
    result = merge_configs(default, override)
    
    assert result["value"] == "new"
    assert result["keep"] == "same"


def test_merge_configs_nested_dicts():
    from dfe_engine.pipeline.pipeline_util import merge_configs
    
    # Test deeply nested dictionary merging
    default = {"level1": {"level2": {"value": "old", "keep": "same"}}}
    override = {"level1": {"level2": {"value": "new", "add": "extra"}}}
    result = merge_configs(default, override)
    
    assert result["level1"]["level2"]["value"] == "new"
    assert result["level1"]["level2"]["keep"] == "same"
    assert result["level1"]["level2"]["add"] == "extra"


# Test gather_env_variables_for_pipeline with meta field
def test_gather_env_variables_for_pipeline_with_meta(sample_package_config):
    # Add meta field to pipeline config
    sample_package_config["ingestion_pipelines"]["test-pipeline"]["meta"] = {
        "META_VAR": "meta_value",
        "CUSTOM_VAR": "overridden_by_meta"  # This should override the env var
    }
    
    env_vars, _ = gather_env_variables_for_pipeline(sample_package_config, "test-pipeline")
    assert "META_VAR" in env_vars
    assert env_vars["META_VAR"] == "meta_value"
    assert env_vars["CUSTOM_VAR"] == "overridden_by_meta"  # Meta should override env


# Test read_vector_step_config with non-existent file
def test_read_vector_step_config_file_not_found(sample_package_config):
    # Remove the actual file to test file not found error
    import os
    sources_dir = sample_package_config["global_settings"]["vector_files"]["sources"]
    test_file = os.path.join(sources_dir, "0-source-step.yml")
    
    # Temporarily rename the file
    temp_name = test_file + ".backup"
    os.rename(test_file, temp_name)
    
    try:
        with pytest.raises(PipelineSchemaError) as exc:
            read_vector_step_config("0-source-step.yml", sample_package_config)
        assert "not found. Make sure your template is located in either" in str(exc.value)
    finally:
        # Restore the file
        os.rename(temp_name, test_file)
