import re
import tempfile
import textwrap
import warnings
from pathlib import Path

import pytest
from hyperi_pylib.logger import logger
from jinja2 import Environment, FileSystemLoader

from dfe_engine.hunts.hunt import Hunt


@pytest.fixture(scope="session")
def template_dir_filters() -> Path:
    """Fixture to create Jinja2 templates for testing customer filters."""
    tmpdirname = tempfile.mkdtemp()
    template_path = Path(tmpdirname)

    # 1. Single line rule
    rule_template_path = template_path / "single_line_rule.jinja2"
    rule_template_path.write_text(
        "INSERT INTO {{ org_id }}.{{ target_table_name }} SELECT * FROM {{ org_id }}.{{ source_table_name }} "
        "WHERE {timestamp_condition} AND {customer_filters}"
    )

    # 2. Multi-line rule
    rule_template_path = template_path / "multi_line_rule.jinja2"
    rule_template_path.write_text(
        textwrap.dedent("""
        INSERT INTO {{ org_id }}.{{ target_table_name }} 
        SELECT * FROM {{ org_id }}.{{ source_table_name }} 
        WHERE {timestamp_condition} 
        AND {customer_filters}
    """)
    )

    # 3. No customer filters
    rule_template_path = template_path / "no_filters.jinja2"
    rule_template_path.write_text(
        "INSERT INTO {{ org_id }}.{{ target_table_name }} SELECT * FROM {{ org_id }}.{{ source_table_name }} "
        "WHERE {timestamp_condition}"
    )

    # 4. Two customer filters instances
    rule_template_path = template_path / "two_filters.jinja2"
    rule_template_path.write_text(
        "INSERT INTO {{ org_id }}.{{ target_table_name }} SELECT * FROM {{ org_id }}.{{ source_table_name }} "
        "WHERE {timestamp_condition} AND {customer_filters} OR field='value' AND {customer_filters}"
    )

    # 5. With spaces around (hunts.py handles this)
    rule_template_path = template_path / "spaced_filters.jinja2"
    rule_template_path.write_text(
        "INSERT INTO {{ org_id }}.{{ target_table_name }} SELECT * FROM {{ org_id }}.{{ source_table_name }} "
        "WHERE {timestamp_condition} AND { customer_filters }"
    )

    # 5a. With multiple spaces - keep the multiple spaces to test failure case
    rule_template_path = template_path / "multi_space_filters.jinja2"
    rule_template_path.write_text(
        "INSERT INTO {{ org_id }}.{{ target_table_name }} SELECT * FROM {{ org_id }}.{{ source_table_name }} "
        "WHERE {timestamp_condition} AND {   customer_filters   }"
    )

    # 6. Filter at beginning of WHERE
    rule_template_path = template_path / "beginning_filter.jinja2"
    rule_template_path.write_text(
        "INSERT INTO {{ org_id }}.{{ target_table_name }} SELECT * FROM {{ org_id }}.{{ source_table_name }} "
        "WHERE {customer_filters} AND {timestamp_condition}"
    )

    # 7. Filter at end of WHERE
    rule_template_path = template_path / "end_filter.jinja2"
    rule_template_path.write_text(
        "INSERT INTO {{ org_id }}.{{ target_table_name }} SELECT * FROM {{ org_id }}.{{ source_table_name }} "
        "WHERE {timestamp_condition} AND field='value' AND {customer_filters}"
    )

    # 8. Complex nested condition
    rule_template_path = template_path / "complex_filter.jinja2"
    rule_template_path.write_text(
        "INSERT INTO {{ org_id }}.{{ target_table_name }} SELECT * FROM {{ org_id }}.{{ source_table_name }} "
        "WHERE {timestamp_condition} AND (field1='value1' OR ({customer_filters} AND field2='value2'))"
    )

    # 9. In SQL comments
    rule_template_path = template_path / "comment_filter.jinja2"
    rule_template_path.write_text(
        "INSERT INTO {{ org_id }}.{{ target_table_name }} SELECT * FROM {{ org_id }}.{{ source_table_name }} "
        "WHERE {timestamp_condition} -- AND {customer_filters} this is a comment"
    )

    # 10. In multi-line SQL comments
    rule_template_path = template_path / "multiline_comment_filter1.jinja2"
    rule_template_path.write_text(
        textwrap.dedent("""
        INSERT INTO {{ org_id }}.{{ target_table_name }} 
        SELECT * FROM {{ org_id }}.{{ source_table_name }}
        WHERE {timestamp_condition} 
        AND (
            /* This is a multi-line comment
               that spans multiple lines and contains
               the customer filters: {customer_filters}
               which should be properly replaced */
            field1 = 'value1'
            AND field2 = 'value2'
        )
    """)
    )

    # 11. Another multi-line SQL test
    rule_template_path = template_path / "multiline_comment_filter2.jinja2"
    rule_template_path.write_text(
        textwrap.dedent("""
        INSERT INTO {{ org_id }}.{{ target_table_name }} 
        SELECT * FROM {{ org_id }}.{{ source_table_name }}
        WHERE {timestamp_condition} 
        AND (
            /* This is a multi-line comment
            that spans multiple lines  */
            field1 = 'value1'
            AND field2 = 'value2'
            AND {customer_filters}
        )
    """)
    )

    yield template_path

    for file in template_path.glob("*"):
        file.unlink()
    template_path.rmdir()


@pytest.mark.parametrize(
    ("rule_name", "filter_clause", "expected_content", "unexpected_content"),
    [
        # 1. Basic cases
        ("single_line_rule", "field='test'", "(field='test')", "{customer_filters}"),
        ("single_line_rule", "", "True", "{customer_filters}"),
        # 2. Multi-line rule
        ("multi_line_rule", "field='test'", "(field='test')", "{customer_filters}"),
        ("multi_line_rule", "", "True", "{customer_filters}"),
        # 3. No customer filters in template
        ("no_filters", "field='test'", "{timestamp_condition}", "{customer_filters}"),
        # 4. Multiple filter instances
        (
            "two_filters",
            "field='test'",
            "AND (field='test') OR field='value' AND (field='test')",
            "{customer_filters}",
        ),
        ("two_filters", "", "AND True OR field='value' AND True", "{customer_filters}"),
        # 5. Spaced filters - supported in hunts.py
        ("spaced_filters", "field='test'", "(field='test')", "{ customer_filters }"),
        # 5a. Multiple spaces - testing that it fails
        (
            "multi_space_filters",
            "field='test'",
            "{   customer_filters   }",
            "(field='test')",
        ),
        # 6-7. Position in WHERE clause
        (
            "beginning_filter",
            "field='test'",
            "(field='test') AND",
            "{customer_filters}",
        ),
        (
            "end_filter",
            "field='test'",
            "AND field='value' AND (field='test')",
            "{customer_filters}",
        ),
        # 8. Complex nested condition
        (
            "complex_filter",
            "field='test'",
            "(field1='value1' OR ((field='test') AND field2='value2'))",
            "{customer_filters}",
        ),
        (
            "complex_filter",
            "",
            "(field1='value1' OR (True AND field2='value2'))",
            "{customer_filters}",
        ),
        # 9. In comments - current implementation actually replaces in comments too
        (
            "comment_filter",
            "field='test'",
            "-- AND (field='test')",
            "{customer_filters}",
        ),
        # 10. In multi-line comments
        (
            "multiline_comment_filter1",
            "field='test'",
            "/* This is a multi-line comment\n       that spans multiple lines and contains\n       the customer filters: (field='test')",
            "{customer_filters}",
        ),
        # 11. In multi-line comments
        (
            "multiline_comment_filter2",
            "field='test'",
            "/* This is a multi-line comment\n    that spans multiple lines",
            "{customer_filters}",
        ),
    ],
)
def test_customer_filters_processing(
    rule_name,
    filter_clause,
    expected_content,
    unexpected_content,
    template_dir_filters,
    setup_paths,
    target_config_data,
):
    """Test various customer filter processing scenarios."""
    env = Environment(loader=FileSystemLoader(template_dir_filters))
    org_id = "detectionlab"

    customer_filters = {}
    if filter_clause:
        customer_filters = {
            org_id: {"rules": [{"name": rule_name, "filter_clause": filter_clause}]}
        }

    hunt = Hunt(
        name="Test Customer Filters",
        cron="* * * * *",
        customer=org_id,
        log_buffer=60,
        thread_id="test123",
        rules=[{"rule_name": rule_name}],
        global_source_table_name="logs_source",
        global_target_table_name="logs_target",
        checkpoint_timestamp_field="timestamp",
        customer_filters=customer_filters,
        hunt_log_path=str(setup_paths[1]),
        target_config_data=target_config_data,
        checkpoint_destination="clickhouse",
        hunt_checkpoint_path=str(setup_paths[4]),
    )

    hunt.convert_yaml_to_sql(env, org_id=org_id, customer_filters=customer_filters)
    assert len(hunt.queries_by_customer) == 1, (
        f"Expected 1 query, found {len(hunt.queries_by_customer)}"
    )
    generated_sql = hunt.queries_by_customer[org_id][0]

    logger.info(f"TEST [{rule_name}] GENERATED SQL: \n{generated_sql}")

    assert expected_content in generated_sql, (
        f"Expected '{expected_content}' not found in generated SQL: {generated_sql}"
    )
    assert unexpected_content not in generated_sql, (
        f"Unexpected '{unexpected_content}' found in generated SQL: {generated_sql}"
    )

    if rule_name == "multi_space_filters":
        assert "{   customer_filters   }" in generated_sql, (
            f"Expected unprocessed placeholder to remain in SQL: {generated_sql}"
        )
    elif rule_name != "comment_filter":
        assert "{customer_filters}" not in generated_sql, (
            f"Unprocessed placeholder found in SQL: {generated_sql}"
        )
        assert "{ customer_filters }" not in generated_sql, (
            f"Unprocessed placeholder found in SQL: {generated_sql}"
        )


def test_multiline_filter_clause_processing(template_dir_filters, setup_paths, target_config_data):
    """Test specifically for multiline filter clauses."""
    env = Environment(loader=FileSystemLoader(template_dir_filters))
    org_id = "detectionlab"
    rule_name = "single_line_rule"

    multiline_filter = """field1='value1'
    OR field2='value2'
    OR field3='value3'
    """

    customer_filters = {org_id: {"rules": [{"name": rule_name, "filter_clause": multiline_filter}]}}

    hunt = Hunt(
        name="Test Multiline Filter",
        cron="* * * * *",
        customer=org_id,
        log_buffer=60,
        thread_id="test123",
        rules=[{"rule_name": rule_name}],
        global_source_table_name="logs_source",
        global_target_table_name="logs_target",
        checkpoint_timestamp_field="timestamp",
        customer_filters=customer_filters,
        hunt_log_path=str(setup_paths[1]),
        target_config_data=target_config_data,
        checkpoint_destination="clickhouse",
        hunt_checkpoint_path=str(setup_paths[4]),
    )

    hunt.convert_yaml_to_sql(env, org_id=org_id, customer_filters=customer_filters)
    assert len(hunt.queries_by_customer) == 1
    generated_sql = hunt.queries_by_customer[org_id][0]

    assert "field1='value1'" in generated_sql
    assert "field2='value2'" in generated_sql
    assert "field3='value3'" in generated_sql

    assert "{customer_filters}" not in generated_sql, (
        f"Unprocessed placeholder found in SQL: {generated_sql}"
    )

    expected_multiline_pattern = (
        r"\(field1='value1'\s*\n\s*OR field2='value2'\s*\n\s*OR field3='value3'\s*\n\s*\)"
    )
    assert re.search(expected_multiline_pattern, generated_sql, re.DOTALL), (
        f"Expected multiline pattern not found in SQL: {generated_sql}"
    )


def test_multiline_filter_clause_with_comments(
    template_dir_filters, setup_paths, target_config_data
):
    """Test specifically for multiline filter clauses containing SQL comments."""
    env = Environment(loader=FileSystemLoader(template_dir_filters))
    org_id = "detectionlab"
    rule_name = "single_line_rule"

    multiline_filter = """field1='value1'
    OR field2='value2'
    /* comment here */
    OR field3='value3'
    -- comment here 
    """

    customer_filters = {org_id: {"rules": [{"name": rule_name, "filter_clause": multiline_filter}]}}

    hunt = Hunt(
        name="Test Multiline Filter with Comments",
        cron="* * * * *",
        customer=org_id,
        log_buffer=60,
        thread_id="test123",
        rules=[{"rule_name": rule_name}],
        global_source_table_name="logs_source",
        global_target_table_name="logs_target",
        checkpoint_timestamp_field="timestamp",
        customer_filters=customer_filters,
        hunt_log_path=str(setup_paths[1]),
        target_config_data=target_config_data,
        checkpoint_destination="clickhouse",
        hunt_checkpoint_path=str(setup_paths[4]),
    )

    hunt.convert_yaml_to_sql(env, org_id=org_id, customer_filters=customer_filters)
    assert len(hunt.queries_by_customer) == 1
    generated_sql = hunt.queries_by_customer[org_id][0]

    assert "field1='value1'" in generated_sql
    assert "field2='value2'" in generated_sql
    assert "field3='value3'" in generated_sql

    assert "/* comment here */" in generated_sql
    assert "-- comment here" in generated_sql
    assert "{customer_filters}" not in generated_sql

    filter_pattern = r"\(field1='value1'\s+OR field2='value2'\s+/\* comment here \*/\s+OR field3='value3'\s+-- comment here"
    assert re.search(filter_pattern, generated_sql, re.DOTALL), (
        f"Expected pattern not found in SQL: {generated_sql}"
    )


def test_multiple_spaces_limitation(template_dir_filters, setup_paths, target_config_data):
    """Test to document the limitation in handling placeholders with multiple spaces."""
    env = Environment(loader=FileSystemLoader(template_dir_filters))
    org_id = "detectionlab"
    rule_name = "multi_space_filters"
    filter_clause = "field='test'"

    customer_filters = {org_id: {"rules": [{"name": rule_name, "filter_clause": filter_clause}]}}

    hunt = Hunt(
        name="Test Multiple Spaces Limitation",
        cron="* * * * *",
        customer=org_id,
        log_buffer=60,
        thread_id="test123",
        rules=[{"rule_name": rule_name}],
        global_source_table_name="logs_source",
        global_target_table_name="logs_target",
        checkpoint_timestamp_field="timestamp",
        customer_filters=customer_filters,
        hunt_log_path=str(setup_paths[1]),
        target_config_data=target_config_data,
        checkpoint_destination="clickhouse",
        hunt_checkpoint_path=str(setup_paths[4]),
    )

    template = env.get_template(f"{rule_name}.jinja2")
    template_content = template.render(
        org_id=org_id,
        target_table_name=hunt.global_target_table_name,
        source_table_name=hunt.global_source_table_name,
        timestamp_condition="{timestamp_condition}",
        customer_filters="{   customer_filters   }",
    )
    logger.info(f"TEMPLATE BEFORE PROCESSING:\n{template_content}")
    logger.info(f"FILTER CLAUSE TO BE APPLIED: {filter_clause}")

    hunt.convert_yaml_to_sql(env, org_id=org_id, customer_filters=customer_filters)
    generated_sql = hunt.queries_by_customer[org_id][0]

    logger.warning(f"SQL WITH MULTI-SPACE LIMITATION:\n{generated_sql}")

    assert "{   customer_filters   }" in generated_sql, (
        "The current implementation should not handle multiple spaces"
    )
    assert "field='test'" not in generated_sql, (
        "The filter clause should not be applied when there are multiple spaces"
    )

    warning_message = (
        "LIMITATION DETECTED: The current implementation only handles {customer_filters} (no spaces) "
        "or { customer_filters } (exactly one space on each side). "
        "Multiple spaces like {   customer_filters   } are not supported and will not be processed."
    )
    logger.warning(warning_message)
    warnings.warn(warning_message, UserWarning, stacklevel=2)

    expected_sql = generated_sql.replace("{   customer_filters   }", f"({filter_clause})")
    logger.info(f"EXPECTED SQL IF IMPLEMENTED CORRECTLY:\n{expected_sql}")

    assert "{   customer_filters   }" in generated_sql, (
        "⚠️ LIMITATION: Customer filters with multiple spaces are not processed"
    )
