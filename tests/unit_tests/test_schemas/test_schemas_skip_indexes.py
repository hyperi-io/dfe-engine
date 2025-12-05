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
def test_derived_schema_precedence(dfe_config_fixtures, create_schema, use_subsampling):
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
            }
        ]
    )
    add_fields_df = pd.DataFrame(
        columns=["column", "type", "index_type", "index_order", "os_order", "comment"]
    )
    log_schema_config(
        meta_schema_df, derived_schema_df, "test_derived_schema_precedence - Before"
    )
    schema = create_schema(
        meta_schema_df,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=use_subsampling,
    )
    log_schema_state(schema, "Before Build")
    schema.build_clickhouse_schema()
    log_schema_state(schema, "After Build")
    sql_file = list(Path(schema.schema_output_path).glob("*.sql"))[0]
    sql_content = sql_file.read_text()
    logger.debug("\nGenerated SQL:")
    logger.debug("\n" + sql_content)
    assert "INDEX idx_category category TYPE set(0) GRANULARITY 4" in sql_content
    
    lines = sql_content.split("\n")
    order_by_line = None
    for line in lines:
        if "ORDER BY" in line and "PROJECTION" not in line:
            order_by_line = line
            break
    
    assert order_by_line is not None, "No main ORDER BY clause found"
    assert "category" in order_by_line

    if use_subsampling:
        assert "cityHash64(timestamp_load)" in order_by_line
        assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content
    else:
        assert "cityHash64(timestamp_load)" not in order_by_line
        assert "SAMPLE BY" not in sql_content


@pytest.mark.parametrize("use_subsampling", [False, True])
def test_invalid_index_order_handling(
    dfe_config_fixtures, create_schema, use_subsampling
):
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
                "index_order": None,
                "os_order": 2,
                "comment": "category",
            }
        ]
    )
    add_fields_df = pd.DataFrame(
        columns=["column", "type", "index_type", "index_order", "os_order", "comment"]
    )

    log_schema_config(
        meta_schema_df, derived_schema_df, "test_invalid_index_order_handling - Before"
    )

    schema = create_schema(
        meta_schema_df,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=use_subsampling,
    )

    log_schema_state(schema, "Before Build")

    schema.build_clickhouse_schema()

    log_schema_state(schema, "After Build")

    sql_file = list(Path(schema.schema_output_path).glob("*.sql"))[0]
    sql_content = sql_file.read_text()
    logger.debug("\nGenerated SQL:")
    logger.debug("\n" + sql_content)

    assert "INDEX idx_category category TYPE set(0) GRANULARITY 4" in sql_content

    lines = sql_content.split("\n")
    order_by_line = None
    for line in lines:
        if "ORDER BY" in line and "PROJECTION" not in line:
            order_by_line = line
            break
    
    assert order_by_line is not None, "No main ORDER BY clause found"
    assert "category" in order_by_line

    if use_subsampling:
        assert "cityHash64(timestamp_load)" in order_by_line
        assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content
    else:
        assert "cityHash64(timestamp_load)" not in order_by_line
        assert "SAMPLE BY" not in sql_content


@pytest.mark.parametrize("use_subsampling", [False, True])
def test_range_index_creation(dfe_config_fixtures, create_schema, use_subsampling):
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
                "column": "port",
                "type": "int16",
                "index_type": "",
                "index_order": 2,
                "os_order": 2,
                "comment": "port number",
            },
        ]
    )

    derived_schema_df = pd.DataFrame(
        [
            {
                "column": "port",
                "type": "int16",
                "index_type": "range",
                "index_order": 2,
                "os_order": 2,
                "comment": "port number",
            }
        ]
    )

    add_fields_df = pd.DataFrame(
        columns=["column", "type", "index_type", "index_order", "os_order", "comment"]
    )

    log_schema_config(
        meta_schema_df, derived_schema_df, "test_range_index_creation - Before"
    )

    schema = create_schema(
        meta_schema_df,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=use_subsampling,
    )
    log_schema_state(schema, "Before Build")

    schema.build_clickhouse_schema()
    log_schema_state(schema, "After Build")

    sql_file = list(Path(schema.schema_output_path).glob("*.sql"))[0]
    sql_content = sql_file.read_text()

    logger.debug("\nGenerated SQL:")
    logger.debug("\n" + sql_content)

    assert "INDEX idx_port port TYPE minmax GRANULARITY 4" in sql_content

    if use_subsampling:
        assert "cityHash64(timestamp_load)" in sql_content
        assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content
    else:
        assert "cityHash64(timestamp_load)" not in sql_content
        assert "SAMPLE BY" not in sql_content


@pytest.mark.parametrize("use_subsampling", [False, True])
def test_range_index_creation_column_name_correct(
    dfe_config_fixtures, create_schema, use_subsampling
):
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
                "column": "port.number",
                "type": "int16",
                "index_type": "",
                "index_order": 2,
                "os_order": 2,
                "comment": "port number",
            },
        ]
    )

    derived_schema_df = pd.DataFrame(
        [
            {
                "column": "port.number",
                "type": "int16",
                "index_type": "dimension",
                "index_order": 2,
                "os_order": 2,
                "comment": "port number",
            }
        ]
    )

    add_fields_df = pd.DataFrame(
        columns=["column", "type", "index_type", "index_order", "os_order", "comment"]
    )

    log_schema_config(
        meta_schema_df, derived_schema_df, "test_range_index_creation - Before"
    )

    schema = create_schema(
        meta_schema_df,
        derived_schema_df,
        add_fields_df,
        use_subsampling_feature=use_subsampling,
    )
    log_schema_state(schema, "Before Build")

    schema.build_clickhouse_schema()
    log_schema_state(schema, "After Build")

    sql_file = list(Path(schema.schema_output_path).glob("*.sql"))[0]
    sql_content = sql_file.read_text()

    logger.debug("\nGenerated SQL:")
    logger.debug("\n" + sql_content)

    assert "INDEX idx_port_number port_number TYPE set(0) GRANULARITY 4" in sql_content

    if use_subsampling:
        assert "cityHash64(timestamp_load)" in sql_content
        assert "SAMPLE BY cityHash64(timestamp_load)" in sql_content
    else:
        assert "cityHash64(timestamp_load)" not in sql_content
        assert "SAMPLE BY" not in sql_content


@pytest.mark.parametrize("use_subsampling", [False, True])
def test_order_by_precedence(dfe_config_fixtures, create_schema, use_subsampling):
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

    lines = sql_content.split("\n")
    order_by_line = None
    for line in lines:
        if "ORDER BY" in line and "PROJECTION" not in line:
            order_by_line = line
            break
    
    assert order_by_line is not None, "No main ORDER BY clause found"
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

    if use_subsampling:
        assert order_by_columns[0] == "cityHash64(timestamp_load)", (
            "cityHash64(timestamp_load) must be first in ORDER BY when subsampling is enabled"
        )
        assert order_by_columns[1] == "timestamp_load", (
            "timestamp_load must be second in ORDER BY"
        )
        assert order_by_columns[2:] == ["event_id", "category"], (
            "User-defined columns should follow timestamp_load in correct order"
        )
    else:
        assert order_by_columns[0] == "timestamp_load", (
            "timestamp_load must be first in ORDER BY when subsampling is disabled"
        )
        assert order_by_columns[1:] == ["event_id", "category"], (
            "User-defined columns should follow timestamp_load in correct order"
        )
