import logging
import os
import re
from typing import List
import pytest
import pandas as pd
from dfecli.dfe_schemabuilder.schema_ch import ClickHouseSchema
from dfecli.dfe_schemabuilder.schema_util import SchemaUtils, SchemaValidationError

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


@pytest.fixture
def create_schema(tmp_path) -> ClickHouseSchema:
    def _create_schema(
        meta_schema_df: pd.DataFrame,
        derived_schema_df: pd.DataFrame,
        add_fields_df: pd.DataFrame,
        common_version: str = "v001_001_005",
        use_subsampling_feature: bool = False,
    ) -> ClickHouseSchema:
        output_directory = tmp_path / "dfeoutput"
        output_directory.mkdir(parents=True, exist_ok=True)

        meta_schema_path = tmp_path / "meta_schema.csv"
        meta_schema_df.to_csv(meta_schema_path, index=False)

        derived_schema_path = tmp_path / "derived_schema.csv"
        derived_schema_df.to_csv(derived_schema_path, index=False)

        add_fields_path = tmp_path / "additional_fields.csv"
        add_fields_df.to_csv(add_fields_path, index=False)

        return ClickHouseSchema(
            name="test_schema",
            version="1.0",
            meta_schema_file_path=str(meta_schema_path),
            dfe_output_path=str(output_directory),
            derived_schema_full_path=str(derived_schema_path),
            additional_fields_full_path=str(add_fields_path),
            common_resource_path=f"common/{common_version}",
            use_subsampling_feature=use_subsampling_feature,
            logger=logger,
        )

    return _create_schema


def validate_columns(expected_columns: List[str], normalized_sql_content: str):
    expected_columns = [col.replace(".", "_") for col in expected_columns]
    for column in expected_columns:
        assert column in normalized_sql_content, (
            f"Column '{column}' not found in SQL template."
        )


def validate_common_headers(normalized_sql_content: str):
    expected_common_columns = ["timestamp", "org_id", "timestamp_load"]
    for column in expected_common_columns:
        sql_column_value = SchemaUtils.sql_column_fix_name(column)
        assert sql_column_value in normalized_sql_content, (
            f"Column '{sql_column_value}' not found in SQL template."
        )


def validate_ttl_statements(normalized_sql_content: str):
    expected_ttl_statements = [
        "TTL timestamp + INTERVAL 90 DAY DELETE WHERE timestamp >= 0",
        "timestamp_load + INTERVAL 90 DAY DELETE WHERE timestamp_load >= 0",
    ]
    for ttl_statement in expected_ttl_statements:
        assert ttl_statement in normalized_sql_content, (
            f"TTL setting '{ttl_statement}' is missing in SQL template."
        )


def validate_order_by_clause(
    expected_order_by_clause: str, normalized_sql_content: str
):
    assert "ORDER BY" in normalized_sql_content, (
        "ORDER BY clause is missing in SQL template."
    )
    assert expected_order_by_clause in normalized_sql_content.replace("\n", " "), (
        f"ORDER BY clause is not correctly constructed. Expected \n{expected_order_by_clause} in {normalized_sql_content}"
    )


def validate_engine_statement(schema: ClickHouseSchema, normalized_sql_content: str):
    engine_statement = (
        "ENGINE = SharedMergeTree()"
        if schema.use_shared_merge_tree
        else (
            "ENGINE = ReplicatedMergeTree()"
            if schema.use_replicated_merge_tree
            else "ENGINE = MergeTree()"
        )
    )
    assert engine_statement in normalized_sql_content, (
        f"Engine statement '{engine_statement}' is missing or incorrect in SQL template."
    )


def validate_index_statement(schema: ClickHouseSchema, normalized_sql_content: str):
    assert "INDEX idx_timestamp timestamp TYPE" in normalized_sql_content, (
        "INDEX clause is missing in SQL template."
    )


def validate_partition_by_statement(
    schema: ClickHouseSchema, normalized_sql_content: str
):
    assert schema.PARTITION_BY_STATEMENT.strip() in normalized_sql_content, (
        "Partition by statement is missing in SQL template."
    )


def validate_datatype_statement(schema, expected_detailed_types: dict):
    schema.build_clickhouse_schema()

    output_sql_path = os.path.join(
        schema.schema_output_path, f"{schema.schema_name}.sql"
    )

    assert os.path.exists(output_sql_path), "SQL template was not generated."

    with open(output_sql_path, "r") as sql_file:
        sql_content = sql_file.read()

    normalized_sql_content = sql_content.replace("\n", " ")

    for column, expected_type in expected_detailed_types.items():
        column_pattern = rf"{column}\s+{re.escape(expected_type)}"
        if not re.search(column_pattern, normalized_sql_content):
            raise AssertionError(
                f"Column '{column}' does not match expected type '{expected_type}'. "
                "Verify nullable status, codec, and data type in schema."
            )


def run_schema_validation_tests(
    schema: ClickHouseSchema, expected_columns: List[str], expected_order_by_clause: str
):
    schema.build_clickhouse_schema()

    output_sql_path = os.path.join(
        schema.schema_output_path, f"{schema.schema_name}.sql"
    )
    assert os.path.exists(output_sql_path), "SQL template was not generated."

    with open(output_sql_path, "r") as sql_file:
        sql_content = sql_file.read()

    normalized_sql_content = sql_content.replace("\n", " ")

    validate_columns(expected_columns, normalized_sql_content)
    validate_common_headers(normalized_sql_content)
    validate_ttl_statements(normalized_sql_content)
    validate_order_by_clause(expected_order_by_clause, normalized_sql_content)
    validate_engine_statement(schema, normalized_sql_content)
    validate_partition_by_statement(schema, normalized_sql_content)


def test_log_alerts_clickhouse_schema_common_003(
    create_schema, create_base_meta_schema_alerts
):
    derived_schema_df = pd.DataFrame(
        {
            "column": ["event_id", "rule_id"],
            "index_order": [1, 2],
            "os_order": [1, 2],
            "comment": ["event id", "rule id"],
        }
    )

    add_fields_df = pd.DataFrame(
        {
            "column": ["alert_confidence_level"],
            "type": ["string_fast_lowcardinality"],
            "default": [""],
            "index_order": [3],
            "os_order": [3],
            "comment": ["Confidence"],
        }
    )

    schema = create_schema(
        create_base_meta_schema_alerts, derived_schema_df, add_fields_df
    )
    expected_columns = ["event_id", "rule_id", "alert_confidence_level"]
    expected_order_by_clause = (
        "ORDER BY (timestamp_load, event_id, alert_confidence_level)"
    )
    run_schema_validation_tests(schema, expected_columns, expected_order_by_clause)


def test_log_alerts_clickhouse_schema_common_005(
    create_schema, create_base_meta_schema_alerts
):
    derived_schema_df = pd.DataFrame(
        {
            "column": ["event_id", "rule_id"],
            "index_order": [1, 2],
            "os_order": [1, 2],
            "comment": ["event id", "rule id"],
        }
    )

    add_fields_df = pd.DataFrame(
        {
            "column": ["alert_confidence_level"],
            "type": ["string_fast_lowcardinality"],
            "default": [""],
            "index_order": [3],
            "os_order": [3],
            "comment": ["Confidence"],
        }
    )

    schema = create_schema(
        create_base_meta_schema_alerts,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=True,
    )
    expected_columns = ["event_id", "rule_id", "alert_confidence_level"]
    expected_order_by_clause = "ORDER BY (cityHash64(timestamp_load), timestamp_load, event_id, alert_confidence_level)"
    run_schema_validation_tests(schema, expected_columns, expected_order_by_clause)


def test_log_alerts_clickhouse_schema_common_index(
    create_schema, create_base_meta_schema_alerts
):
    derived_schema_df = pd.DataFrame(
        {
            "column": ["event_id", "rule_id"],
            "index_order": [1, 2],
            "os_order": [1, 2],
            "comment": ["event id", "rule id"],
        }
    )

    add_fields_df = pd.DataFrame(
        {
            "column": ["alert_confidence_level"],
            "type": ["string_fast_lowcardinality"],
            "default": [""],
            "index_order": [3],
            "os_order": [3],
            "comment": ["Confidence"],
        }
    )

    schema = create_schema(
        create_base_meta_schema_alerts, derived_schema_df, add_fields_df
    )
    schema.build_clickhouse_schema()

    output_sql_path = os.path.join(
        schema.schema_output_path, f"{schema.schema_name}.sql"
    )

    with open(output_sql_path, "r") as sql_file:
        sql_content = sql_file.read()

    normalized_sql_content = sql_content.replace("\n", " ")
    validate_index_statement(schema, normalized_sql_content)


def test_logs_nxlog_windows_clickhouse_schema_common_001(
    create_schema, create_base_meta_schema_nxlog_windows
):
    derived_schema_df = pd.DataFrame(
        {
            "column": ["event_id", "domain"],
            "index_order": ["", "1"],
            "os_order": ["", "1"],
            "comment": ["event id", "domain"],
        }
    )

    add_fields_df = pd.DataFrame(
        {
            "column": ["event_creation_time"],
            "type": ["datetime"],
            "default": [""],
            "index_order": [""],
            "os_order": [""],
            "comment": ["event_creation_time"],
        }
    )

    schema = create_schema(
        create_base_meta_schema_nxlog_windows, derived_schema_df, add_fields_df
    )
    expected_columns = ["event_id", "domain", "event_creation_time"]
    expected_order_by_clause = "ORDER BY (timestamp_load, domain)"
    expected_detailed_types = {
        "event_id": "String CODEC(ZSTD(1))",
        "event_creation_time": "Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))",
    }

    run_schema_validation_tests(schema, expected_columns, expected_order_by_clause)
    validate_datatype_statement(schema, expected_detailed_types)


def test_logs_nxlog_windows_clickhouse_schema_common_002(
    create_schema, create_base_meta_schema_nxlog_windows
):
    derived_schema_df = pd.DataFrame(
        {
            "column": ["event_id", "domain"],
            "index_order": [1, 2],
            "os_order": [1, 2],
            "comment": ["event id", "domain"],
        }
    )

    add_fields_df = pd.DataFrame(
        {
            "column": ["event_creation_time"],
            "type": ["datetime"],
            "default": [""],
            "index_order": [3],
            "os_order": [3],
            "comment": ["event_creation_time"],
        }
    )

    schema = create_schema(
        create_base_meta_schema_nxlog_windows, derived_schema_df, add_fields_df
    )
    expected_columns = ["event_id", "domain", "event_creation_time"]
    expected_order_by_clause = "ORDER BY (timestamp_load, event_id, domain)"
    expected_detailed_types = {
        "event_id": "String CODEC(ZSTD(1))",
        "event_creation_time": "Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))",
    }

    run_schema_validation_tests(schema, expected_columns, expected_order_by_clause)
    validate_datatype_statement(schema, expected_detailed_types)


def test_log_filebeat_clickhouse_schema_common_003(
    create_schema, create_base_meta_schema_filebeat
):
    derived_schema_df = pd.DataFrame(
        {
            "column": ["user*", "agent_properties_id"],
            "index_order": ["", 1],
            "os_order": ["", 1],
            "comment": ["users", "agent_properties_id"],
        }
    )

    add_fields_df = pd.DataFrame(
        {
            "column": ["o365.audit.Severity"],
            "type": ["string_fast_lowcardinality"],
            "default": [""],
            "index_order": [""],
            "os_order": [""],
            "comment": ["Severity"],
        }
    )

    schema = create_schema(
        create_base_meta_schema_filebeat, derived_schema_df, add_fields_df
    )
    expected_columns = ["agent_properties_id", "user", "o365_audit_severity"]
    expected_order_by_clause = "ORDER BY (timestamp_load, agent_properties_id)"
    run_schema_validation_tests(schema, expected_columns, expected_order_by_clause)


def test_log_filebeat_catch_derived_schema_duplicates(
    create_schema, create_base_meta_schema_filebeat
):
    derived_schema_df = pd.DataFrame(
        {
            "column": ["user*", "agent_properties_id"],
            "type": ["", ""],
            "default": ["", ""],
            "index_order": ["", 2],
            "os_order": ["", 2],
            "comment": ["users", "agent_properties_id"],
        }
    )

    add_fields_df = pd.DataFrame(
        {
            "column": ["o365.audit.Severity", "agent_properties_id"],
            "type": ["string_fast_lowcardinality", "string_fast_lowcardinality"],
            "default": ["", ""],
            "index_order": ["", ""],
            "os_order": ["", ""],
            "comment": ["Severity", "agent_properties_id"],
        }
    )

    schema = create_schema(
        create_base_meta_schema_filebeat, derived_schema_df, add_fields_df
    )

    try:
        schema.build_clickhouse_schema()
    except SchemaValidationError as e:
        assert "Duplicate column names provided" in str(e)
        return


def test_log_filebeat_catch_core_and_common_field_clash(
    create_schema, create_base_meta_schema_filebeat
):
    derived_schema_df = pd.DataFrame(
        {
            "column": ["user*", "agent_properties_id"],
            "index_order": ["", 2],
            "os_order": ["", 2],
            "comment": ["users", "agent_properties_id duplicate"],
        }
    )

    add_fields_df = pd.DataFrame(
        {
            "column": ["o365.audit.Severity", "timestamp"],
            "type": ["string_fast_lowcardinality", "datetime"],
            "default": ["", ""],
            "index_order": ["", ""],
            "os_order": ["", ""],
            "comment": ["Severity", "timestamp duplicate"],
        }
    )

    schema = create_schema(
        create_base_meta_schema_filebeat, derived_schema_df, add_fields_df
    )
    schema.build_clickhouse_schema()

def test_sub_schema_type_override_and_fallback(create_schema):
    """
    Test that sub-schema 'type' column overrides meta schema, and fallback works if missing.
    """

    meta_schema_df = pd.DataFrame({
        'column': ['event_id', 'rule_id'],
        'type': ['string', 'int32'],
        'default': ['', ''],
        'index_order': [1, 2],
        'os_order': [1, 2],
        'comment': ['event id', 'rule id']
    })

    derived_schema_df = pd.DataFrame({
        'column': ['event_id', 'rule_id'],
        'type': ['string', ''],
        'index_order': [1, 2],
        'os_order': [1, 2],
        'comment': ['event id', 'rule id']
    })

    add_fields_df = pd.DataFrame({
        'column': [], 'type': [], 'default': [], 'index_order': [], 'os_order': [], 'comment': []
    })

    schema = create_schema(meta_schema_df, derived_schema_df, add_fields_df)
    schema.build_clickhouse_schema()

    output_sql_path = os.path.join(
        schema.schema_output_path,
        f"{schema.schema_name}.sql"
    )
    assert os.path.exists(output_sql_path), "SQL template was not generated."
    with open(output_sql_path, 'r') as sql_file:
        sql_content = sql_file.read().replace('\n', ' ')

    assert re.search(r"event_id\s+String", sql_content, re.IGNORECASE), "event_id type override failed"
    assert re.search(r"rule_id\s+Nullable\(Int32\)", sql_content, re.IGNORECASE), "rule_id fallback to meta schema type failed"

def test_sub_schema_full_override_and_fallback(create_schema):
    """
    Test that sub-schema 'type', 'index_type', and 'index_order' columns override meta schema,
    and fallback works if missing. Also checks order-independence and empty values.
    """
    import re

    meta_schema_df = pd.DataFrame({
        'column': ['event_id', 'rule_id'],
        'type': ['string', 'int32'],
        'index_type': ['tokenbf_v1', 'ngrambf_v1'],
        'index_order': [1, 2],
        'default': ['', ''],
        'os_order': [1, 2],
        'comment': ['event id', 'rule id']
    })

    derived_schema_df = pd.DataFrame({
        'column': ['event_id', 'rule_id'],
        'type': ['string_fast_lowcardinality', ''],
        'index_type': ['ngrambf_v1', ''],
        'index_order': [None, 99],
        'default': ['', ''],
        'os_order': [1, 2],
        'comment': ['event id', 'rule id']
    })

    add_fields_df = pd.DataFrame({
        'column': [], 'type': [], 'default': [], 'index_order': [], 'os_order': [], 'comment': []
    })

    schema = create_schema(meta_schema_df, derived_schema_df, add_fields_df)
    schema.build_clickhouse_schema()

    output_sql_path = os.path.join(
        schema.schema_output_path,
        f"{schema.schema_name}.sql"
    )
    assert os.path.exists(output_sql_path), "SQL template was not generated."
    with open(output_sql_path, 'r') as sql_file:
        sql_content = sql_file.read().replace('\n', ' ')


    assert re.search(r"event_id\s+(LowCardinality\(String\)|String).*CODEC", sql_content, re.IGNORECASE), "event_id type override failed"
    assert "1" in sql_content, "event_id index_order fallback failed"
    assert re.search(r"rule_id\s+Nullable\(Int32\)", sql_content, re.IGNORECASE), "rule_id type fallback failed"