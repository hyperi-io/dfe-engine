import re

import pytest
from sigma.collection import SigmaCollection
from sigma.rule import SigmaRule

from dfe_engine.sigma.sigma_backend_clickhouse import SqlBackend


@pytest.fixture
def clickhouse_backend():
    return SqlBackend()


def normalize_sql_query(query):
    """Normalize a SQL query for comparison: strip leading/trailing whitespace,
    reduce internal whitespace to single spaces, remove line breaks."""
    query = re.sub(r"\s+", " ", query)
    return query.strip()


def format_clickhouse_insert_query(
    rule: SigmaRule, query: str, tactics: str, techniques: str
) -> str:
    """
    Formats and returns a ClickHouse INSERT query statement for inserting alerts into the database.

    Parameters:
    - rule: SigmaRule object containing rule details.
    - query: String containing the WHERE clause conditions to filter the alerts.
    - tactics: Comma-separated string of tactics IDs.
    - techniques: Comma-separated string of techniques IDs.

    Returns:
    - A formatted ClickHouse INSERT query string.
    """
    org_id = "{{ org_id }}"
    target_table = "{{ target_table_name }}"
    source_table = "{{ source_table_name }}"

    clickhouse_insert_query = (
        f"INSERT INTO {org_id}.{target_table} ("
        f"alert_description, "
        f"alert_framework, "
        f"alert_ratingtime_sla_applies, "
        f"alert_rule_name, "
        f"alert_schedule, "
        f"alert_schedule_duration, "
        f"alert_severity, "
        f"alert_triage_score, "
        f"alert_type, "
        f"detected_time, "
        f"logoriginal, "
        f"org_id, "
        f"source_table, "
        f"tactics, "
        f"techniques, "
        f"timestamp) "
        f"SELECT "
        f"'{rule.description}', "
        f"'MITRE ATT&CK', "
        f"'true', "
        f"'{rule.title}', "
        f"'1m', "
        f"100, "
        f"'{rule.level}', "
        f"100, "
        f"'scheduled alert', "
        f"now(), "
        f"'logoriginal', "
        f"'{org_id}', "
        f"'{source_table}', "
        f"'{tactics}', "
        f"'{techniques}', "
        f" timestamp "
        f"FROM {org_id}.{source_table} "
        f"WHERE {query} "
    ).strip()

    return clickhouse_insert_query


def test_clickhouse_and_expression(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            sel:
                fieldA: valueA
                fieldB: valueB
            condition: sel
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = ["(fieldA = 'valueA' AND fieldB = 'valueB')"]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_clickhouse_or_expression(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            sel1:
                fieldA: valueA
            sel2:
                fieldB: valueB
            condition: 1 of sel*
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = ["(fieldA = 'valueA' OR fieldB = 'valueB')"]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_clickhouse_and_or_expression(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            sel:
                fieldA:
                    - valueA1
                    - valueA2
                fieldB:
                    - valueB1
                    - valueB2
            condition: sel
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = [
        "((fieldA = 'valueA1' OR fieldA = 'valueA2') AND (fieldB = 'valueB1' OR fieldB = 'valueB2'))"
    ]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_clickhouse_or_and_expression(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            sel1:
                fieldA: valueA1
                fieldB: valueB1
            sel2:
                fieldA: valueA2
                fieldB: valueB2
            condition: 1 of sel*
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = [
        "((fieldA = 'valueA1' AND fieldB = 'valueB1') OR (fieldA = 'valueA2' AND fieldB = 'valueB2'))"
    ]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_clickhouse_or_wildcard_expression(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            sel:
                fieldA:
                    - valueA
                    - valueB
                    - valueC*
            condition: sel
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = ["(fieldA = 'valueA' OR fieldA = 'valueB' OR fieldA ILIKE 'valueC%')"]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_clickhouse_regex_query(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            sel:
                fieldA|re: foo.*bar
                fieldB: foo
            condition: sel
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = ["(match(fieldA, 'foo.*bar') AND fieldB = 'foo')"]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_clickhouse_cidr_query(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            selection:
                Image|endswith: '\\\\rundll32.exe'
                Initiated: 'true'
            filter_main_local_ranges:
                DestinationIp|cidr:
                    - '192.168.0.0/16'
            condition: selection and not 1 of filter_main_*
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))

    assert "Image ILIKE" in generated_query[0]
    assert "rundll32.exe" in generated_query[0]
    assert "Initiated = 'true'" in generated_query[0]
    assert "NOT" in generated_query[0]
    assert "cidrmatch(DestinationIp, '192.168.0.0/16')" in generated_query[0]


def test_clickhouse_cidr_query_multiple_values(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test Multiple CIDRs
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            selection:
                Image: 'test.exe'
                DestinationIp|cidr:
                    - '192.168.0.0/16'
                    - '10.0.0.0/8'
                    - '172.16.0.0/12'
            condition: selection
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))

    assert "Image = 'test.exe'" in generated_query[0]
    assert "cidrmatch(DestinationIp, '192.168.0.0/16')" in generated_query[0]
    assert "cidrmatch(DestinationIp, '10.0.0.0/8')" in generated_query[0]
    assert "cidrmatch(DestinationIp, '172.16.0.0/12')" in generated_query[0]


def test_clickhouse_field_name_with_whitespace(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            sel:
                'field name': value
            condition: sel
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = ["'field\\ name' = 'value'"]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_wildcard_ilike(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        author: frack113
        title: VC Lee Rule
        date: 2021-07-21
        modified: 2023-10-27
        tags:
            - attack.command_and_control
            - attack.t1095
        logsource:
            product: logs_nxlog_windows
            category: ps_classic_start
        detection:
            selection:
                Channel|contains:
                    - '*Security*'
                    - '*powercat.ps1*'
            condition: selection
        falsepositives:
            - Unknown
        level: medium
    """
    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = ["(Channel ILIKE '%Security%' OR Channel ILIKE '%powercat.ps1%')"]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_full_alert_format_with_metadata(clickhouse_backend: SqlBackend):
    """Test alert generation with metadata from both global defaults and rule-specific settings."""
    sigma_yaml = """
        title: PsExec Default Named Pipe
        id: f3f3a972-f982-40ad-b63c-bca6afdfad7c
        status: test
        description: Detects PsExec service default pipe creation
        author: Test Author
        references:
            - https://example.com/ref1
        tags:
            - attack.execution
            - attack.t1569.002
            - detection.threat_hunting
        logsource:
            product: windows
            service: security
        detection:
            selection:
                EventID: 1234
                Image: test_image.exe
            condition: selection
        level: high
    """

    alert_metadata = {
        "alert_type": "Suspicious Process",
        "alert_severity": "high",
        "alert_triage_score": 80,
        "alert_description": "Custom description",
        "alert_schedule": "smd",
        "alert_schedule_duration": "10mins",
        "alert_ratingtime_sla_applies": "true",
        "alert_framework": "MITRE ATT&CK",
    }
    clickhouse_backend.alert_metadata = alert_metadata

    converted_query = clickhouse_backend.convert(
        SigmaCollection.from_yaml(sigma_yaml), output_format="full_alert"
    )

    assert "'Suspicious Process'" in converted_query
    assert "'high'" in converted_query
    assert "80" in converted_query
    assert "'Custom description'" in converted_query
    assert "'smd'" in converted_query
    assert "'MITRE ATT&CK'" in converted_query
    assert "'execution, threat_hunting'" in converted_query
    assert "'T1569.002'" in converted_query


def test_field_mapping_layers(clickhouse_backend: SqlBackend):
    """Test handling of field mappings from different layers."""
    sigma_yaml = """
        title: Test Field Mappings
        id: 2c0a7d9c-6c41-478a-a01c-97d71e7d89be
        status: test
        logsource:
            product: windows
            service: security
        detection:
            selection:
                Image: test.exe
                CommandLine: whoami
                ParentImage: cmd.exe
            condition: selection
    """

    field_mappings = {
        "Image": "process_name",
        "CommandLine": "command_line",
        "ParentImage": "parent_process_name",
    }
    clickhouse_backend.field_mappings = field_mappings

    converted_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))

    assert "process_name = 'test.exe'" in converted_query[0]
    assert "command_line = 'whoami'" in converted_query[0]
    assert "parent_process_name = 'cmd.exe'" in converted_query[0]


def test_mitre_attack_extraction(clickhouse_backend: SqlBackend):
    """Test extraction and formatting of MITRE ATT&CK tactics and techniques."""
    sigma_yaml = """
        title: Test MITRE Extraction
        id: 2c0a7d9c-6c41-478a-a01c-97d71e7d89be
        status: test
        logsource:
            category: test_category
            product: test_product
        tags:
            - attack.execution
            - attack.t1059.001
            - attack.persistence
            - attack.t1547
            - attack.defense_evasion
        detection:
            selection:
                EventID: 1
            condition: selection
    """

    converted_query = clickhouse_backend.convert(
        SigmaCollection.from_yaml(sigma_yaml), output_format="full_alert"
    )

    assert "'execution, persistence, defense_evasion'" in converted_query
    assert "'T1059.001, T1547'" in converted_query


def test_clickhouse_format1_output(clickhouse_backend: SqlBackend):
    """Test for output format format1."""
    sigma_yaml = """
        title: Test Format1
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            sel1:
                fieldA: valueA
            sel2:
                fieldB: valueB
            condition: 1 of sel*
    """
    rules = SigmaCollection.from_yaml(sigma_yaml)
    converted_queries = clickhouse_backend.convert(rules, output_format="format1")
    expected_query = "(fieldA = 'valueA' OR fieldB = 'valueB')"
    assert normalize_sql_query(converted_queries) == normalize_sql_query(expected_query)


def test_clickhouse_format2_output(clickhouse_backend: SqlBackend):
    """Test for output format format2."""
    sigma_yaml = """
        title: Test Format2
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            sel1:
                fieldA: valueA
            sel2:
                fieldB: valueB
            condition: sel1 and sel2
    """
    rules = SigmaCollection.from_yaml(sigma_yaml)
    converted_queries = clickhouse_backend.convert(rules, output_format="format2")
    expected_query = "(fieldA = 'valueA' AND fieldB = 'valueB')"
    assert normalize_sql_query(converted_queries) == normalize_sql_query(expected_query)


def test_text_search_field_handling():
    """Test handling of text fields with text_search index type."""
    schema_metadata = {"message": {"type": "text", "index_type": "text_search"}}
    backend = SqlBackend(schema_metadata=schema_metadata)

    sigma_yaml = """
        title: Test Text Search
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            selection:
                message: 'error'
            condition: selection
    """

    generated_query = backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = ["(message GLOBAL IN INDEX idx_ngram_bf 'error' AND message ILIKE '%error%')"]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_text_field_without_text_search():
    """Test handling of text fields without text_search index type."""
    schema_metadata = {"message": {"type": "text", "index_type": "default"}}
    backend = SqlBackend(schema_metadata=schema_metadata)

    sigma_yaml = """
        title: Test Text Field
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            selection:
                message: 'error'
            condition: selection
    """

    generated_query = backend.convert(SigmaCollection.from_yaml(sigma_yaml))
    expected_query = ["message = 'error'"]
    assert normalize_sql_query(generated_query[0]) == normalize_sql_query(expected_query[0])


def test_malformed_cidr_handling(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test Malformed CIDR
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            selection:
                DestinationIp|cidr: '999.168.0.0/16'
            condition: selection
    """
    try:
        generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))
        assert "selection" in str(generated_query)
    except Exception as e:
        assert "CIDR" in str(e) or "IP" in str(e)


def test_nested_field_mappings(clickhouse_backend: SqlBackend):
    field_mappings = {
        "User": "event.user.name",
        "Source.Address": "event.source.address",
        "Source.Port": "event.source.port",
        "Target.Address": "event.destination.address",
        "Target.Port": "event.destination.port",
    }
    clickhouse_backend.field_mappings = field_mappings

    sigma_yaml = """
        title: Test Nested Fields
        status: test
        logsource:
            category: network
            product: firewall
        detection:
            selection:
                User: 'admin'
                Source.Address: '192.168.1.1'
                Source.Port: 22
                Target.Address: '10.0.0.1'
                Target.Port: 445
            condition: selection
    """

    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))

    assert "event.user.name = 'admin'" in generated_query[0]
    assert "event.source.address" in generated_query[0]
    assert "192.168.1.1" in generated_query[0]
    assert "event.source.port = '22'" in generated_query[0]
    assert "event.destination.address" in generated_query[0]
    assert "10.0.0.1" in generated_query[0]
    assert "event.destination.port = '445'" in generated_query[0]


def test_multi_value_field_mappings(clickhouse_backend: SqlBackend):
    field_mappings = {
        "URL": ["url.full", "url.original"],
        "IP": ["source.ip", "client.ip"],
    }
    clickhouse_backend.field_mappings = field_mappings

    sigma_yaml = """
        title: Test Multi-value Field Mappings
        status: test
        logsource:
            category: webserver
            product: apache
        detection:
            selection:
                URL: 'admin.php'
                IP: '1.2.3.4'
            condition: selection
    """

    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))

    assert "(url.full = 'admin.php' OR url.original = 'admin.php')" in generated_query[0]

    assert "source.ip" in generated_query[0]
    assert "client.ip" in generated_query[0]
    assert "1.2.3.4" in generated_query[0]
    assert "OR" in generated_query[0]


class TestLiteralEscaping:
    """F-SIGMA-ESCAPING: every value interpolated into a single-quoted ClickHouse
    literal must escape the backslash BEFORE the quote, so a crafted value cannot
    break out of the literal. Each site routes through _escape_value."""

    def test_escape_value_backslash_quote_wildcard(self, clickhouse_backend):
        # quote doubled
        assert clickhouse_backend._escape_value("a'b") == "a''b"
        # backslash doubled (was the gap - CH honours C-style backslash escapes)
        assert clickhouse_backend._escape_value("a\\b") == "a\\\\b"
        # backslash FIRST then quote: a lone \' cannot break out
        assert clickhouse_backend._escape_value("\\'") == "\\\\''"
        # wildcards are literal-safe and preserved (LIKE handling is separate)
        assert clickhouse_backend._escape_value("a*b%c") == "a*b%c"

    def test_match_expression_escapes_quote(self, clickhouse_backend):
        out = clickhouse_backend._create_match_expression("f", "x' OR '1'='1")
        assert out == "match(f, 'x'' OR ''1''=''1')"
        # the lone single quote never survives to break the literal
        assert "x' OR" not in out

    def test_match_expression_escapes_backslash(self, clickhouse_backend):
        assert clickhouse_backend._create_match_expression("f", "a\\'b") == "match(f, 'a\\\\''b')"

    def test_regex_condition_routes_through_escape(self, clickhouse_backend):
        # A plain regex value is unchanged (no quote/backslash) - existing behaviour.
        assert (
            clickhouse_backend._create_match_expression("f", "foo.*bar") == "match(f, 'foo.*bar')"
        )

    def test_cidr_expression_escapes(self, clickhouse_backend):
        assert clickhouse_backend._create_cidr_expression("f", "x'y") == "cidrmatch(f, 'x''y')"
        # a normal CIDR literal is unchanged
        assert (
            clickhouse_backend._create_cidr_expression("f", "10.0.0.0/8")
            == "cidrmatch(f, '10.0.0.0/8')"
        )

    def test_like_expression_escapes_backslash_and_quote(self, clickhouse_backend):
        assert (
            clickhouse_backend._create_like_expression("f", "a\\'b", "contains")
            == "f ILIKE '%a\\\\''b%'"
        )

    def test_text_search_branch_escapes(self):
        backend = SqlBackend(
            schema_metadata={"message": {"type": "text", "index_type": "text_search"}}
        )
        out = backend._handle_field_value_expression("message", "x'y")
        assert "idx_ngram_bf 'x''y'" in out
        assert "ILIKE '%x''y%'" in out
        # no unescaped single quote breaks the literal
        assert "'x'y'" not in out
