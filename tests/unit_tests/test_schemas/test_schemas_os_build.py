import logging
import os
from typing import List
import pytest
import json
import pandas as pd
from dfecli.dfe_schemabuilder.schema_os import OpenSearchTemplate
from pathlib import Path

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


@pytest.fixture
def create_opensearch_template(tmp_path) -> OpenSearchTemplate:
    """Fixture to create an OpenSearchTemplate instance with predefined settings."""

    def _create_template(
        template_resource_path: str,
        derived_schema_df: pd.DataFrame,
        add_fields_df: pd.DataFrame,
        common_resource_path: str,
        source_template_type="xde_custom",
    ) -> OpenSearchTemplate:
        output_directory = tmp_path / "dfeoutput"
        output_directory.mkdir(parents=True, exist_ok=True)

        derived_schema_path = tmp_path / "derived_schema.csv"
        add_fields_path = tmp_path / "additional_fields.csv"
        derived_schema_df.to_csv(derived_schema_path, index=False)
        add_fields_df.to_csv(add_fields_path, index=False)

        return OpenSearchTemplate(
            name="logs_beats_filebeat",
            version="1",
            meta_schema_file_path=template_resource_path,
            common_resource_path=common_resource_path,
            derived_schema_full_path=str(derived_schema_path),
            additional_fields_full_path=str(add_fields_path),
            dfe_output_path=str(output_directory),
            source_template_type=source_template_type,
            logger=logger,
        )

    return _create_template


def validate_template_meta_fields(generated_template: dict, expected_description: str):
    """Validate the meta fields of the OpenSearch template."""
    meta = generated_template.get("_meta", {})
    assert "description" in meta, "Missing 'description' in '_meta'."
    assert expected_description in meta["description"], (
        f"Expected description '{expected_description}' not found in the '_meta' field."
    )


def validate_template_basic_fields(
    generated_template: dict, expected_index_patterns: List[str]
):
    """Validate the basic fields of the OpenSearch template."""
    assert "composed_of" in generated_template, (
        "Missing 'composed_of' field in OpenSearch template."
    )
    assert generated_template["composed_of"] == ["hypersec-log-component-template"], (
        "'composed_of' does not match expected value."
    )

    assert "priority" in generated_template, (
        "Missing 'priority' field in OpenSearch template."
    )
    pattern = expected_index_patterns[0].replace("*", "")
    expected_priority = str(100 + (len(pattern.split("-")) * 10))
    assert generated_template["priority"] == expected_priority, (
        "'priority' field does not match expected value."
    )

    data_stream = generated_template.get("data_stream", {})
    assert "timestamp_field" in data_stream, (
        "Missing 'timestamp_field' in 'data_stream'."
    )
    assert data_stream["timestamp_field"].get("name") == "@timestamp", (
        "'timestamp_field' does not match expected value."
    )

    assert "index_patterns" in generated_template, (
        "Missing 'index_patterns' field in OpenSearch template."
    )
    assert generated_template["index_patterns"] == expected_index_patterns, (
        "'index_patterns' do not match expected values."
    )


def validate_template_settings(generated_template: dict):
    """Validate the settings of the OpenSearch template."""
    settings = generated_template.get("template", {}).get("settings", {})
    assert "mapping.total_fields.limit" in settings, (
        "Missing 'mapping.total_fields.limit' in settings."
    )
    assert settings["mapping.total_fields.limit"] == 10000, (
        "'mapping.total_fields.limit' does not match expected value."
    )

    assert "index" in settings, "Missing 'index' in settings."
    assert "query" in settings["index"], "Missing 'query' in index settings."
    assert "default_field" in settings["index"]["query"], (
        "Missing 'default_field' in query settings."
    )


def validate_properties_fields(expected_fields: list, properties: dict):
    """Validate that all the expected properties fields are present in the OpenSearch template properties."""
    for field in expected_fields:
        ref = properties
        keys = field.split(".")

        for key in keys:
            assert key in ref, (
                f"Field '{field}' not found in properties. Current properties: {ref}"
            )
            ref = ref[key]

            if isinstance(ref, dict) and "properties" in ref:
                ref = ref["properties"]


def run_template_validation_tests(template: OpenSearchTemplate, expected_fields: list):
    """Run all validation tests for a generated OpenSearch template."""
    template.build_opensearch_template()

    output_json_path = os.path.join(
        template.schema_output_path, f"{template.schema_name}_opensearch_template.json"
    )
    assert os.path.exists(output_json_path), "OpenSearch template was not generated."

    with open(output_json_path, "r") as json_file:
        generated_template = json.load(json_file)

    expected_description = f"HyperSec {template.schema_name} OpenSearch ISM log streaming template. v{template.version}"
    validate_template_meta_fields(generated_template, expected_description)

    expected_index_patterns = [
        f"{template.schema_name.replace('_', '-')}-*",
        f".ds-{template.schema_name.replace('_', '-')}-*",
    ]
    validate_template_basic_fields(generated_template, expected_index_patterns)
    validate_template_settings(generated_template)

    mappings = generated_template.get("template", {}).get("mappings", {})
    assert "properties" in mappings, (
        "Missing 'properties' field in OpenSearch template."
    )

    validate_properties_fields(expected_fields, mappings["properties"])


def test_opensearch_template_generation(
    dfe_config_fixtures, create_opensearch_template
):
    """Test OpenSearch template generation for proper schema and fields."""
    from pathlib import Path
    repo_root = Path(__file__).parent.parent.parent.parent
    template_resource_path = repo_root / "tests/resources/test_schemas/stable_schemas/logs_beats_filebeat/v001_000_002/logs_beats_filebeat.csv"
    derived_schema_df = pd.DataFrame(
        {
            "column": [
                "activemq_properties_caller",
                "activemq_properties_log_properties_stack_trace",
            ],
            "type": ["string_fast", "string_fast"],
            "index_order": ["", ""],
            "os_order": ["", ""],
            "comment": ["test tes", "tes tes"],
        }
    )
    add_fields_df = pd.DataFrame(
        {
            "column": ["test_add"],
            "type": ["string_fast"],
            "index_order": [""],
            "os_order": [""],
            "comment": ["network egress bytes"],
        }
    )

    template = create_opensearch_template(
        template_resource_path=template_resource_path,
        derived_schema_df=derived_schema_df,
        add_fields_df=add_fields_df,
        common_resource_path="common/v001_001_005",
    )

    expected_fields = [
        "activemq_properties_caller",
        "activemq_properties_log_properties_stack_trace",
        "test_add",
        "@timestamp",
        "timestamp_collector",
        "timestamp_load",
        "timestamp_received",
        "timestamp_finalise",
        "timestamp_epochms",
        "timestamp_collector_epochms",
        "timestamp_load_epochms",
        "timestamp_received_epochms",
        "timestamp_finalise_epochms",
        "event_hash",
        "logoriginal",
        "org_id",
        "tags.collector.host",
        "tags.collector.hostname",
        "tags.collector.source",
        "tags.collector.timestamp",
        "tags.collector.timezone",
        "tags.event.category",
        "tags.event.org_id",
        "tags.event.site_id",
        "tags.event.type",
        "tags.event.error",
    ]

    run_template_validation_tests(template, expected_fields)
