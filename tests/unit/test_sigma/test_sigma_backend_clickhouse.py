#  Project:      dfe-engine
#  File:         tests/unit/test_sigma/test_sigma_backend_clickhouse.py
#  Purpose:      What the engine adds on top of the pysigma-backend-clickhouse backend
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine's choices over the package backend: bare condition, field quoting, text index.

The package owns the Sigma semantics. These cases pin only what the subclass changes,
plus hostile values, because a value reaches ClickHouse as a literal in a rule an
operator did not write.
"""

import re
import textwrap

import pytest
from sigma.collection import SigmaCollection
from sigma.exceptions import SigmaFeatureNotSupportedByBackendError

from dfe_engine.sigma.sigma_backend_clickhouse import SqlBackend

HOSTILE_VALUES = [
    "O'Brien",
    "back\\slash",
    "x' OR '1'='1",
    "a\\' OR 1=1 --",
    "100%_done",
]
TEXT_INDEXED = {"type": "text", "use_case": "substring_search"}
TEXT_INDEXED_WORDS = {"type": "text", "use_case": "word_search"}


def convert(detection: str, schema_metadata: dict | None = None, condition: str = "selection"):
    """Convert one rule whose ``selection`` is *detection* and return the queries."""
    sigma_yaml = f"""
        title: Test
        status: test
        logsource:
            category: test_category
            product: test_product
        detection:
            selection:
                {detection}
            condition: {condition}
    """
    backend = SqlBackend(schema_metadata=schema_metadata or {})
    return backend.convert(SigmaCollection.from_yaml(sigma_yaml))


def where(detection: str, schema_metadata: dict | None = None) -> str:
    """The single condition a one-selection rule converts to."""
    (query,) = convert(detection, schema_metadata)
    return query


@pytest.mark.parametrize(
    ("detection", "expected"),
    [
        pytest.param("fieldA: valueA", "fieldA='valueA'", id="equality"),
        pytest.param("Image|endswith: '.exe'", "Image ILIKE '%.exe'", id="endswith"),
        pytest.param(
            "ip|cidr: '10.0.0.0/8'",
            "(toIPv6OrNull(toString(ip)) IS NOT NULL"
            " AND isIPAddressInRange(toString(ip), '10.0.0.0/8'))",
            id="cidr",
        ),
        pytest.param("Image|re: '^a.b$'", "match(Image, '^a.b$')", id="regex"),
        pytest.param("n: null", "isNull(n)", id="null"),
        pytest.param("EventID: 1", "EventID='1'", id="number_as_text"),
        pytest.param("EventID: [1, 2]", "EventID IN ('1', '2')", id="numbers_in_a_list"),
        pytest.param("flag: true", "flag=true", id="bool"),
        pytest.param("CommandLine: 'a?b'", "CommandLine ILIKE 'a_b'", id="single_wildcard"),
        pytest.param(
            "CommandLine|contains: 'a_b'", "CommandLine ILIKE '%a\\\\_b%'", id="underscore"
        ),
    ],
)
def test_a_detection_converts_to_a_bare_condition(detection: str, expected: str):
    """The condition carries no ``SELECT``, because the hunt wraps it in its own query."""
    assert where(detection) == expected


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        pytest.param("EventID", "EventID", id="plain"),
        pytest.param("event.user.name", "event.user.name", id="dotted_path_stays_bare"),
        pytest.param("field name", "`field name`", id="space"),
        pytest.param("a`b", "`a``b`", id="backtick_doubled"),
        pytest.param("x = 1 OR 1", "`x = 1 OR 1`", id="sql_in_name"),
        pytest.param("null", "`null`", id="literal_word"),
    ],
)
def test_a_field_name_renders_as_a_column_reference(field: str, expected: str):
    """A field name is a column reference through the engine's quoting, never SQL."""
    assert where(f"'{field}': v") == f"{expected}='v'"


# A string literal as ClickHouse lexes it: a doubled quote or a backslash escape stays inside.
_LITERAL = re.compile(r"'(?:[^'\\]|''|\\.)*'")


@pytest.mark.parametrize(
    ("modifier", "skeleton"),
    [
        ("", ("message='?'", "message ILIKE '?'")),
        ("|contains", ("message ILIKE '?'",)),
        ("|startswith", ("message ILIKE '?'",)),
        ("|endswith", ("message ILIKE '?'",)),
        ("|re", ("match(message, '?')",)),
    ],
)
def test_a_hostile_value_stays_inside_its_literal(modifier: str, skeleton: tuple[str, ...]):
    """With every well-formed literal blanked, only the operator's own SQL is left.

    A value that closed its literal early would leave its text in the skeleton.
    """
    for needle in HOSTILE_VALUES:
        assert _LITERAL.sub("'?'", where(f"message{modifier}: {needle!r}")) in skeleton


def test_a_correlation_rule_is_refused():
    """A correlation rule renders a whole query, which cannot be a WHERE condition."""
    correlation = textwrap.dedent("""
        title: base
        name: base
        logsource: {category: c, product: p}
        detection:
            s: {a: b}
            condition: s
        ---
        title: corr
        correlation:
            type: event_count
            rules: [base]
            group-by: [u]
            timespan: 5m
            condition: {gte: 3}
    """)
    with pytest.raises(NotImplementedError):
        SqlBackend().convert(SigmaCollection.from_yaml(correlation))


def test_a_keyword_only_rule_is_refused():
    """There is no full-text column to search, so a bare keyword has nothing to match."""
    with pytest.raises(SigmaFeatureNotSupportedByBackendError):
        convert("- needle", condition="selection")


@pytest.mark.parametrize(
    ("detection", "expected"),
    [
        ("message|contains: 'PowerShell'", "lower(message) LIKE '%powershell%'"),
        ("message|startswith: 'PowerShell'", "lower(message) LIKE 'powershell%'"),
        ("message|endswith: 'PowerShell'", "lower(message) LIKE '%powershell'"),
        ("message: '*PowerShell*'", "lower(message) LIKE '%powershell%'"),
        ("message: 'Power*Shell*'", "lower(message) LIKE 'power%shell%'"),
        ('message|contains: "O\'Brien"', "lower(message) LIKE '%o''brien%'"),
        ("message|contains: 'back\\slash'", "lower(message) LIKE '%back\\\\\\\\slash%'"),
        ("message|contains: \"x' OR '1'='1\"", "lower(message) LIKE '%x'' or ''1''=''1%'"),
        ("message: 'error'", "message='error'"),
    ],
)
@pytest.mark.parametrize("column", [TEXT_INDEXED, TEXT_INDEXED_WORDS], ids=["substring", "word"])
def test_a_text_indexed_column_is_matched_through_lower(detection: str, expected: str, column):
    """ILIKE reads every granule: ClickHouse prunes on a text index for LIKE only.

    Both sides fold case, so the needle is lowered with the column. A value with no
    wildcard stays an equality.
    """
    assert where(detection, {"message": column}) == expected


@pytest.mark.parametrize("use_case", ["substring_search", "word_search", "text_search", "fulltext"])
def test_every_text_index_use_case_reaches_the_lower_path(use_case: str):
    """Including the retired spellings: a schema stored before the rename carries them."""
    metadata = {"message": {"type": "text", "use_case": use_case}}

    assert where("message|contains: 'Error'", metadata) == "lower(message) LIKE '%error%'"


@pytest.mark.parametrize("use_case", ["dimension", "exact_match", "range", None, ""])
def test_a_column_with_no_text_index_keeps_ilike(use_case):
    """lower() on an unindexed column buys nothing, so nothing outside the two changes."""
    metadata = {"message": {"type": "text", "use_case": use_case}}

    assert where("message|contains: 'Error'", metadata) == "message ILIKE '%Error%'"


def test_an_unknown_column_keeps_ilike():
    """A field the schema metadata never mentions must not be assumed indexed."""
    assert where("message|contains: 'Error'", {}) == "message ILIKE '%Error%'"


def test_a_quoted_column_is_looked_up_by_its_name_not_its_quoting():
    """The schema metadata is keyed by the column name, never by its backticked form."""
    metadata = {"log message": TEXT_INDEXED}

    assert (
        where("'log message|contains': 'Error'", metadata) == "lower(`log message`) LIKE '%error%'"
    )


def test_the_needle_folds_ascii_only_like_clickhouse_lower():
    """lower() leaves non-ASCII letters alone, so a fully folded needle would never match."""
    assert where("message|contains: 'ÉCOLE'", {"message": TEXT_INDEXED}) == (
        "lower(message) LIKE '%École%'"
    )
