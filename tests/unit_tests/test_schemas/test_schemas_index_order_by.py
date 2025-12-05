import logging
import pytest
import pandas as pd
from pathlib import Path
from dfecli.dfe_schemabuilder.schema_ch import ClickHouseSchema

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def log_schema_config(
    meta_schema_df: pd.DataFrame, derived_schema_df: pd.DataFrame, test_name: str
):
    """Helper function to log debug information about the schemas."""
    logger.debug(f"\n=== {test_name} Debug Info ===")
    logger.debug("\nMeta Schema:")
    logger.debug("\n" + meta_schema_df.to_string())
    logger.debug("\nDerived Schema:")
    logger.debug("\n" + derived_schema_df.to_string())
    logger.debug("\n")


def log_schema_state(schema: ClickHouseSchema, stage: str):
    """Helper function to log schema state at different stages."""
    logger.debug(f"\n=== Schema State at {stage} ===")
    logger.debug("\nMeta Schema:")
    logger.debug("\n" + schema.meta_schema_df.to_string())
    logger.debug("\n")


def parse_order_by_columns(sql_content: str) -> list:
    """Helper function to parse ORDER BY columns from SQL content.

    Args:
        sql_content: The SQL content containing the ORDER BY clause

    Returns:
        List of column names in the ORDER BY clause preserving function calls
    """
    lines = sql_content.split("\n")
    order_by_line = None
    for line in lines:
        if "ORDER BY" in line and "PROJECTION" not in line:
            order_by_line = line
            break
    
    if not order_by_line:
        raise ValueError("No ORDER BY clause found")
        
    order_by_clause = order_by_line.split("ORDER BY")[1].strip()
    order_by_columns_raw = order_by_clause[1:-1].strip()
    order_by_columns = []
    current_part = ""
    paren_count = 0

    for char in order_by_columns_raw:
        if char == "(" and paren_count == 0:
            current_part += char
            paren_count += 1
        elif char == ")" and paren_count > 0:
            current_part += char
            paren_count -= 1
        elif char == "," and paren_count == 0:
            order_by_columns.append(current_part.strip())
            current_part = ""
        else:
            current_part += char

    if current_part:
        order_by_columns.append(current_part.strip())

    return order_by_columns


@pytest.fixture
def create_schema(tmp_path):
    def _create_schema(
        meta_schema_df: pd.DataFrame,
        derived_schema_df: pd.DataFrame,
        add_fields_df: pd.DataFrame,
        use_subsampling_feature: bool = False,
    ) -> ClickHouseSchema:
        output_directory = tmp_path / "dfeoutput"
        output_directory.mkdir(parents=True, exist_ok=True)
        meta_schema_path = tmp_path / "meta_schema.csv"
        meta_schema_df.to_csv(meta_schema_path, index=False)
        derived_schema_path = tmp_path / "derived_schema.csv"
        add_fields_path = tmp_path / "additional_fields.csv"
        derived_schema_df.to_csv(derived_schema_path, index=False)
        add_fields_df.to_csv(add_fields_path, index=False)
        return ClickHouseSchema(
            name="test_schema",
            version="1.0",
            meta_schema_file_path=meta_schema_path,
            dfe_output_path=str(output_directory),
            derived_schema_full_path=str(derived_schema_path),
            additional_fields_full_path=str(add_fields_path),
            common_resource_path="common/v001_001_007",
            use_subsampling_feature=use_subsampling_feature,
            logger=logger,
        )

    return _create_schema


@pytest.mark.parametrize("use_subsampling", [False, True])
def test_common_header_precedence(dfe_config_fixtures, create_schema, use_subsampling):
    """Test that timestamp_load from common headers is always used in ORDER BY with proper hashing."""
    meta_schema_df = pd.DataFrame(
        [
            {
                "column": "event_id",
                "type": "string",
                "index_type": "",
                "index_order": 1,
                "os_order": 1,
                "comment": "event id",
            },
            {
                "column": "category",
                "type": "string",
                "index_type": "",
                "index_order": 2,
                "os_order": 2,
                "comment": "category",
            },
        ]
    )
    derived_schema_df = pd.DataFrame(
        [
            {
                "column": "category",
                "type": "string",
                "index_type": "dimension",
                "index_order": 3,
                "os_order": 2,
                "comment": "category",
            },
            {
                "column": "event_id",
                "type": "string",
                "index_type": "",
                "index_order": 2,
                "os_order": 1,
                "comment": "event id",
            },
        ]
    )
    add_fields_df = pd.DataFrame(
        columns=["column", "type", "index_type", "index_order", "os_order", "comment"]
    )

    schema = create_schema(
        meta_schema_df,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=use_subsampling,
    )
    schema.build_clickhouse_schema()

    sql_file = list(Path(schema.schema_output_path).glob("*.sql"))[0]
    sql_content = sql_file.read_text()

    order_by_columns = parse_order_by_columns(sql_content)

    if use_subsampling:
        assert order_by_columns[0] == "cityHash64(timestamp_load)", (
            "cityHash64(timestamp_load) must be first in ORDER BY when subsampling is enabled"
        )
        assert order_by_columns[1] == "timestamp_load", (
            "timestamp_load must be second in ORDER BY"
        )
    else:
        assert order_by_columns[0] == "timestamp_load", (
            "timestamp_load must be first in ORDER BY when subsampling is disabled"
        )


@pytest.mark.parametrize("use_subsampling", [False, True])
def test_derived_schema_order_precedence(
    dfe_config_fixtures, create_schema, use_subsampling
):
    """Test that derived schema index_order takes precedence over meta schema."""
    meta_schema_df = pd.DataFrame(
        [
            {
                "column": "event_id",
                "type": "string",
                "index_type": "",
                "index_order": 1,
                "os_order": 1,
                "comment": "event id",
            },
            {
                "column": "category",
                "type": "string",
                "index_type": "",
                "index_order": 2,
                "os_order": 2,
                "comment": "category",
            },
            {
                "column": "status",
                "type": "string",
                "index_type": "",
                "index_order": 3,
                "os_order": 3,
                "comment": "status",
            },
        ]
    )
    derived_schema_df = pd.DataFrame(
        [
            {
                "column": "status",
                "type": "string",
                "index_type": "dimension",
                "index_order": 1,
                "os_order": 3,
                "comment": "status",
            },
            {
                "column": "category",
                "type": "string",
                "index_type": "dimension",
                "index_order": 2,
                "os_order": 2,
                "comment": "category",
            },
            {
                "column": "event_id",
                "type": "string",
                "index_type": "",
                "index_order": 3,
                "os_order": 1,
                "comment": "event id",
            },
        ]
    )
    add_fields_df = pd.DataFrame(
        columns=["column", "type", "index_type", "index_order", "os_order", "comment"]
    )

    schema = create_schema(
        meta_schema_df,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=use_subsampling,
    )
    schema.build_clickhouse_schema()

    sql_file = list(Path(schema.schema_output_path).glob("*.sql"))[0]
    sql_content = sql_file.read_text()

    order_by_columns = parse_order_by_columns(sql_content)
    offset = 2 if use_subsampling else 1
    columns_after_timestamp = order_by_columns[offset:]
    assert columns_after_timestamp == ["status", "category", "event_id"], (
        "Columns not in correct order based on derived schema index_order"
    )

    if use_subsampling:
        assert order_by_columns[0] == "cityHash64(timestamp_load)", (
            "cityHash64(timestamp_load) must be first when subsampling is enabled"
        )
        assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content, (
            "SAMPLE BY clause missing when subsampling is enabled"
        )
    else:
        assert "cityHash64(timestamp_load)" not in sql_content, (
            "cityHash64(timestamp_load) should not be present when subsampling is disabled"
        )
        assert "SAMPLE BY" not in sql_content, (
            "SAMPLE BY clause should not be present when subsampling is disabled"
        )


@pytest.mark.parametrize("use_subsampling", [False, True])
def test_invalid_index_order_handling(
    dfe_config_fixtures, create_schema, use_subsampling
):
    """Test handling of invalid index_order values in derived schema."""
    meta_schema_df = pd.DataFrame(
        [
            {
                "column": "event_id",
                "type": "string",
                "index_type": "",
                "index_order": 1,
                "os_order": 1,
                "comment": "event id",
            },
            {
                "column": "category",
                "type": "string",
                "index_type": "",
                "index_order": 2,
                "os_order": 2,
                "comment": "category",
            },
        ]
    )
    derived_schema_df = pd.DataFrame(
        [
            {
                "column": "category",
                "type": "string",
                "index_type": "dimension",
                "index_order": pd.NA,
                "os_order": 2,
                "comment": "category",
            },
            {
                "column": "event_id",
                "type": "string",
                "index_type": "",
                "index_order": pd.NA,
                "os_order": 1,
                "comment": "event id",
            },
        ]
    )
    add_fields_df = pd.DataFrame(
        columns=["column", "type", "index_type", "index_order", "os_order", "comment"]
    )

    schema = create_schema(
        meta_schema_df,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=use_subsampling,
    )
    schema.build_clickhouse_schema()

    sql_file = list(Path(schema.schema_output_path).glob("*.sql"))[0]
    sql_content = sql_file.read_text()

    order_by_columns = parse_order_by_columns(sql_content)
    offset = 2 if use_subsampling else 1
    columns_after_timestamp = order_by_columns[offset:]
    assert columns_after_timestamp == ["event_id", "category"], (
        "Original meta schema order should be preserved when derived schema has invalid index_order"
    )

    if use_subsampling:
        assert order_by_columns[0] == "cityHash64(timestamp_load)", (
            "cityHash64(timestamp_load) must be first when subsampling is enabled"
        )
        assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content
    else:
        assert "cityHash64(timestamp_load)" not in sql_content
        assert "SAMPLE BY" not in sql_content


@pytest.mark.parametrize("use_subsampling", [False, True])
def test_missing_index_order_handling(
    dfe_config_fixtures, create_schema, use_subsampling
):
    """Test handling of missing index_order values."""
    meta_schema_df = pd.DataFrame(
        [
            {
                "column": "event_id",
                "type": "string",
                "index_type": "",
                "index_order": 1,
                "os_order": 1,
                "comment": "event id",
            },
            {
                "column": "category",
                "type": "string",
                "index_type": "",
                "os_order": 2,
                "comment": "category",
            },  # Missing index_order
            {
                "column": "status",
                "type": "string",
                "index_type": "",
                "index_order": 3,
                "os_order": 3,
                "comment": "status",
            },
        ]
    )
    derived_schema_df = pd.DataFrame(
        [
            {
                "column": "status",
                "type": "string",
                "index_type": "dimension",
                "os_order": 3,
                "comment": "status",
            },  # Missing index_order
            {
                "column": "category",
                "type": "string",
                "index_type": "dimension",
                "index_order": 2,
                "os_order": 2,
                "comment": "category",
            },
        ]
    )
    add_fields_df = pd.DataFrame(
        columns=["column", "type", "index_type", "index_order", "os_order", "comment"]
    )

    schema = create_schema(
        meta_schema_df,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=use_subsampling,
    )
    schema.build_clickhouse_schema()

    sql_file = list(Path(schema.schema_output_path).glob("*.sql"))[0]
    sql_content = sql_file.read_text()

    order_by_columns = parse_order_by_columns(sql_content)

    offset = 2 if use_subsampling else 1
    assert len(order_by_columns) >= offset, (
        "Should include timestamp_load and optional cityHash64(timestamp_load)"
    )

    if use_subsampling:
        assert order_by_columns[0] == "cityHash64(timestamp_load)", (
            "cityHash64(timestamp_load) must be first when subsampling is enabled"
        )
        assert order_by_columns[1] == "timestamp_load", "timestamp_load must be second"
        assert order_by_columns[2] == "category", (
            "Only category should be included as it has valid index_order"
        )
    else:
        assert order_by_columns[0] == "timestamp_load", (
            "timestamp_load must be first when subsampling is disabled"
        )
        assert order_by_columns[1] == "category", (
            "Only category should be included as it has valid index_order"
        )


@pytest.mark.parametrize("use_subsampling", [False, True])
def test_duplicate_index_order_handling(
    dfe_config_fixtures, create_schema, use_subsampling
):
    """Test handling of duplicate index_order values."""
    meta_schema_df = pd.DataFrame(
        [
            {
                "column": "event_id",
                "type": "string",
                "index_type": "",
                "index_order": 1,
                "os_order": 1,
                "comment": "event id",
            },
            {
                "column": "category",
                "type": "string",
                "index_type": "",
                "index_order": 2,
                "os_order": 2,
                "comment": "category",
            },
            {
                "column": "status",
                "type": "string",
                "index_type": "",
                "index_order": 2,
                "os_order": 3,
                "comment": "status",
            },
        ]
    )
    derived_schema_df = pd.DataFrame(
        [
            {
                "column": "status",
                "type": "string",
                "index_type": "dimension",
                "index_order": 2,
                "os_order": 3,
                "comment": "status",
            },
            {
                "column": "category",
                "type": "string",
                "index_type": "dimension",
                "index_order": 2,
                "os_order": 2,
                "comment": "category",
            },
        ]
    )
    add_fields_df = pd.DataFrame(
        columns=["column", "type", "index_type", "index_order", "os_order", "comment"]
    )

    schema = create_schema(
        meta_schema_df,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=use_subsampling,
    )
    schema.build_clickhouse_schema()

    sql_file = list(Path(schema.schema_output_path).glob("*.sql"))[0]
    sql_content = sql_file.read_text()

    order_by_columns = parse_order_by_columns(sql_content)
    offset = 2 if use_subsampling else 1
    columns_after_timestamp = order_by_columns[offset:]

    if use_subsampling:
        assert order_by_columns[0] == "cityHash64(timestamp_load)", (
            "cityHash64(timestamp_load) must be first when subsampling is enabled"
        )
        assert order_by_columns[1] == "timestamp_load", "timestamp_load must be second"
        assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content
    else:
        assert order_by_columns[0] == "timestamp_load", (
            "timestamp_load must be first when subsampling is disabled"
        )
        assert "SAMPLE BY" not in sql_content

    assert set(columns_after_timestamp) == {"category", "status"}, (
        "Columns with same index_order (category, status) should be included"
    )
    assert len(columns_after_timestamp) == 2, (
        "Should include exactly the columns with highest priority index_order"
    )
