import re

import pytest
from sigma.collection import SigmaCollection

from dfe_engine.sigma.sigma_backend_clickhouse import SqlBackend, sql_field


@pytest.fixture
def clickhouse_backend():
    return SqlBackend()


def normalize_sql_query(query):
    """Normalize a SQL query for comparison: strip leading/trailing whitespace,
    reduce internal whitespace to single spaces, remove line breaks."""
    query = re.sub(r"\s+", " ", query)
    return query.strip()


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
    # Backticks, not single quotes: 'field name' = 'value' compares two constants.
    expected_query = ["`field name` = 'value'"]
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


def convert_one(detection: str, schema_metadata: dict | None = None) -> str:
    """Convert a one-selection rule and return its single query.

    Args:
        detection: The body of ``selection:``, e.g. ``message|contains: 'x'``.
        schema_metadata: Column metadata, in the shape the schema-metadata
            producers emit (type / use_case / attribute).

    Returns:
        The emitted SQL, whitespace-normalised.
    """
    backend = SqlBackend(schema_metadata=schema_metadata or {})
    sigma_yaml = f"""
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            selection:
                {detection}
            condition: selection
    """
    return normalize_sql_query(backend.convert(SigmaCollection.from_yaml(sigma_yaml))[0])


# The two use cases dfe-schemas renders as a ClickHouse text index, and the
# retired spelling of each, which current_use_case translates on the way in.
TEXT_INDEXED = {"type": "text", "use_case": "substring_search"}
TEXT_INDEXED_WORDS = {"type": "text", "use_case": "word_search"}


@pytest.mark.parametrize(
    ("detection", "expected"),
    [
        ("message|contains: 'PowerShell'", "lower(message) LIKE '%powershell%'"),
        ("message|startswith: 'PowerShell'", "lower(message) LIKE 'powershell%'"),
        ("message|endswith: 'PowerShell'", "lower(message) LIKE '%powershell'"),
        ("message: '*PowerShell*'", "lower(message) LIKE '%powershell%'"),
        ("message: 'Power*Shell*'", "lower(message) LIKE 'power%shell%'"),
    ],
)
def test_a_text_indexed_column_is_matched_through_lower(detection: str, expected: str):
    """ILIKE reads every granule: ClickHouse prunes on a text index for LIKE only.

    Both sides fold case, so the needle is lowered with the column.
    """
    assert convert_one(detection, {"message": TEXT_INDEXED}) == expected


@pytest.mark.parametrize("use_case", ["substring_search", "word_search", "text_search", "fulltext"])
def test_every_text_index_use_case_reaches_the_lower_path(use_case: str):
    """Including the retired spellings: a schema stored before the rename carries them."""
    metadata = {"message": {"type": "text", "use_case": use_case}}

    assert convert_one("message|contains: 'Error'", metadata) == "lower(message) LIKE '%error%'"


@pytest.mark.parametrize("use_case", ["dimension", "exact_match", "range", None, ""])
def test_a_column_with_no_text_index_keeps_ilike(use_case):
    """lower() on an unindexed column buys nothing, so nothing outside the two changes."""
    metadata = {"message": {"type": "text", "use_case": use_case}}

    assert convert_one("message|contains: 'Error'", metadata) == "message ILIKE '%Error%'"


def test_an_unknown_column_keeps_ilike():
    """A field the schema metadata never mentions must not be assumed indexed."""
    assert convert_one("message|contains: 'Error'", {}) == "message ILIKE '%Error%'"


def test_a_quoted_column_is_looked_up_by_its_name_not_its_quoting():
    """The schema metadata is keyed by the column name, never by its backticked form."""
    metadata = {"log message": TEXT_INDEXED}

    assert (
        convert_one("'log message|contains': 'Error'", metadata)
        == "lower(`log message`) LIKE '%error%'"
    )


def test_a_quoted_targetobject_field_is_still_matched_as_a_substring():
    assert convert_one("'Registry-TargetObject': 'HKLM'", {}) == (
        "`Registry-TargetObject` ILIKE '%HKLM%'"
    )


def test_an_unmodified_value_on_a_text_indexed_column_stays_an_equality():
    """A Sigma value with no modifier and no wildcard asks for equality, not a substring.

    The retired branch matched %value% here, so reviving it unchanged would have
    turned every rule field on such a column into a substring hunt.
    """
    assert convert_one("message: 'error'", {"message": TEXT_INDEXED}) == "message = 'error'"


@pytest.mark.parametrize(
    ("needle", "expected"),
    [
        ("O'Brien", "lower(message) LIKE '%o''brien%'"),
        ("back\\slash", "lower(message) LIKE '%back\\\\slash%'"),
        ("x' OR '1'='1", "lower(message) LIKE '%x'' or ''1''=''1%'"),
    ],
)
def test_the_lower_path_escapes_the_literal(needle: str, expected: str):
    """F-SIGMA-ESCAPING: the needle still sits inside a single-quoted literal."""
    assert convert_one(f"message|contains: {needle!r}", {"message": TEXT_INDEXED}) == expected


def test_a_word_search_column_is_matched_the_same_way():
    """word_search and substring_search differ in tokenizer, not in the query form."""
    metadata = {"message": TEXT_INDEXED_WORDS}

    assert (
        convert_one("message|contains: 'Mimikatz'", metadata) == "lower(message) LIKE '%mimikatz%'"
    )


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


def test_dotted_field_names_stay_bare_and_numbers_render_quoted(clickhouse_backend: SqlBackend):
    sigma_yaml = """
        title: Test Nested Fields
        status: test
        logsource:
            category: network
            product: firewall
        detection:
            selection:
                event.user.name: 'admin'
                event.source.address: '192.168.1.1'
                event.source.port: 22
                event.destination.port: 445
            condition: selection
    """

    generated_query = clickhouse_backend.convert(SigmaCollection.from_yaml(sigma_yaml))

    assert "event.user.name = 'admin'" in generated_query[0]
    assert "cidrmatch(event.source.address, '192.168.1.1')" in generated_query[0]
    assert "event.source.port = '22'" in generated_query[0]
    assert "event.destination.port = '445'" in generated_query[0]


class TestFieldNameRendering:
    """A field name reaches the generated SQL as a column reference, never as SQL.

    A hunt writer holds rule:write and can already author a WHERE clause, but
    sigma:write is admin-only, so a field name must not be a second way in.
    """

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("EventID", "EventID"),
            ("event.user.name", "event.user.name"),
            ("_json.user.id", "_json.user.id"),
            ("field name", "`field name`"),
            ("field!@#$%", "`field!@#$%`"),
            ('field"quotes"', '`field"quotes"`'),
            ("a`b", "`a``b`"),
            ("a\\b", "`a\\\\b`"),
            ("x = 1 OR 1", "`x = 1 OR 1`"),
            ("ts) OR 1=1 --", "`ts) OR 1=1 --`"),
        ],
    )
    def test_a_field_name_renders_as_a_column_reference(self, field: str, expected: str):
        assert sql_field(field) == expected

    def test_an_injected_field_name_cannot_close_its_quoting(self):
        rendered = sql_field("x` OR 1=1 OR `y")

        assert rendered == "`x`` OR 1=1 OR ``y`"
        assert rendered.count("`") % 2 == 0
