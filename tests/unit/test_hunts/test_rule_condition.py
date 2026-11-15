#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_rule_condition.py
#  Purpose:      A rule condition's shape rules and the WHERE it compiles to
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""RuleCondition holds the builder's input to one shape, and compiles to a quoted WHERE.

The compiled text was run against ClickHouse 26.3 over a table with a ``_json`` column
for the cases in ``COMPILED``, and each matched the rows its test describes.
"""

import pytest
import sqlglot
from pydantic import ValidationError

from dfe_engine.hunts.rule_condition import (
    MAX_LIST_VALUES,
    MAX_TESTS,
    OPERAND_KINDS,
    RuleCondition,
    RuleConditionError,
    compile_condition,
    instant_literal,
)
from dfe_engine.hunts.rule_rewriter import RuleRewriter
from dfe_engine.source.models import MATCH_NESTING_LIMIT

COLUMNS = frozenset({"_org_id", "_timestamp", "event_id", "host", "_json"})


def _compile(body: dict, columns=COLUMNS) -> str:
    return compile_condition(RuleCondition.model_validate(body), columns)


COMPILED = {
    "a JSON path equals text, read as a String": (
        {"field": "user.name", "value": "bob"},
        "(_json.`user`.`name`.:String = 'bob')",
    ),
    "not_equals is a != test, so a row without the field is not matched": (
        {"field": "user.name", "operator": "not_equals", "value": "bob"},
        "(_json.`user`.`name`.:String != 'bob')",
    ),
    "a JSON path compared with a number is tried as each numeric type": (
        {"field": "n", "operator": "gt", "value": 5},
        "(coalesce(_json.`n`.:Int64 > 5, _json.`n`.:UInt64 > 5, _json.`n`.:Float64 > 5))",
    ),
    "a column compared with a number is read as it is": (
        {"field": "event_id", "operator": "lt", "value": 3},
        "(`event_id` < 3)",
    ),
    "a JSON path against a date is parsed, in UTC": (
        {"field": "ts", "operator": "gte", "value": "2026-01-01"},
        "(parseDateTime64BestEffortOrNull(_json.`ts`.:String, 3, 'UTC') "
        ">= toDateTime64('2026-01-01 00:00:00.000', 3, 'UTC'))",
    ),
    "a column against a date-time with an offset is moved to UTC": (
        {"field": "_timestamp", "operator": "gt", "value": "2026-01-02T10:00:00+10:00"},
        "(`_timestamp` > toDateTime64('2026-01-02 00:00:00.000', 3, 'UTC'))",
    ),
    "in on a column lists its texts": (
        {"field": "host", "operator": "in", "value": ["alpha", "beta"]},
        "(`host` IN ('alpha', 'beta'))",
    ),
    "in on a JSON path with numbers is tried as each numeric type": (
        {"field": "n", "operator": "in", "value": [3, 12]},
        "(coalesce(_json.`n`.:Int64 IN (3, 12), _json.`n`.:UInt64 IN (3, 12), "
        "_json.`n`.:Float64 IN (3, 12)))",
    ),
    "starts_with on a JSON path reads a String": (
        {"field": "msg", "operator": "starts_with", "value": "hel"},
        "(startsWith(_json.`msg`.:String, 'hel'))",
    ),
    "ends_with on a column is taken over its text": (
        {"field": "host", "operator": "ends_with", "value": "ma"},
        "(endsWith(toString(`host`), 'ma'))",
    ),
    "includes on a column is taken over its text": (
        {"field": "event_id", "operator": "includes", "value": "3"},
        "(position(toString(`event_id`), '3') > 0)",
    ),
    "exists negated finds rows that lack the field": (
        {"field": "msg", "operator": "exists", "negate": True},
        "NOT (isNotNull(_json.`msg`) AND notEmpty(toString(_json.`msg`)))",
    ),
    "an explicit _json prefix is the same path as a bare one": (
        {"field": "_json.a-b.c", "value": "x"},
        "(_json.`a-b`.`c`.:String = 'x')",
    ),
    "a header column is a column": (
        {"field": "_org_id", "value": "acme-1"},
        "(`_org_id` = 'acme-1')",
    ),
    "groups nest, and negate turns a group into none-of": (
        {
            "all": [
                {"field": "host", "value": "alpha"},
                {
                    "any": [
                        {"field": "event_id", "operator": "lt", "value": 3},
                        {"field": "event_id", "operator": "gt", "value": 9},
                    ],
                    "negate": True,
                },
            ]
        },
        "((`host` = 'alpha') AND NOT ((`event_id` < 3) OR (`event_id` > 9)))",
    ),
}


@pytest.mark.parametrize(("body", "sql"), COMPILED.values(), ids=list(COMPILED))
def test_compiles_to_the_where(body, sql):
    assert _compile(body) == sql


# sqlglot has no typed JSON read (``.:String``), so those cases were run on ClickHouse itself.
PARSEABLE = {name: case for name, case in COMPILED.items() if ".:" not in case[1]}


@pytest.mark.parametrize(("body", "sql"), PARSEABLE.values(), ids=list(PARSEABLE))
def test_the_where_parses_as_clickhouse(body, sql):
    """The text is one boolean expression ClickHouse's grammar reads, as sqlglot reads it."""
    sqlglot.parse_one(f"SELECT 1 FROM t WHERE {sql}", read="clickhouse")


def test_a_field_is_a_column_only_when_the_table_has_it():
    """With the columns unknown only a name starting with an underscore is a column."""
    body = {"field": "host", "value": "alpha"}

    assert _compile(body, columns={"host"}) == "(`host` = 'alpha')"
    assert _compile(body, columns=None) == "(_json.`host`.:String = 'alpha')"
    assert _compile({"field": "_org_id", "value": "a"}, columns=None) == "(`_org_id` = 'a')"


COMPILE_REFUSED = {
    "a header column the table lacks": (
        {"field": "_nothing", "value": "x"},
        "no column '_nothing'",
    ),
    "a path inside a plain column": (
        {"field": "host.name", "value": "x"},
        "names nothing inside it",
    ),
    "the bare JSON column": ({"field": "_json", "operator": "exists"}, "name a path inside _json"),
}


@pytest.mark.parametrize(("body", "message"), COMPILE_REFUSED.values(), ids=list(COMPILE_REFUSED))
def test_a_field_that_does_not_fit_the_table_is_refused(body, message):
    with pytest.raises(RuleConditionError, match=message):
        _compile(body)


HOSTILE_TEXT = [
    "x' OR 1=1 --",
    "x\\' OR 1=1 --",
    "x\\",
    "'); DROP TABLE dfe.main; --",
    "`; SELECT 1 --",
    "a\nb",
    "%_*?",
    "\u2019 '",
]


@pytest.mark.parametrize("text", HOSTILE_TEXT)
@pytest.mark.parametrize(
    "operator", ["equals", "not_equals", "includes", "starts_with", "ends_with"]
)
def test_an_operand_never_leaves_its_quotes(operator, text):
    """Every quote and backslash in a text operand is doubled, so one literal holds it all."""
    sql = _compile({"field": "host", "operator": operator, "value": text})

    statement = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {sql}", read="clickhouse")
    literals = [lit.this for lit in statement.find_all(sqlglot.exp.Literal) if lit.is_string]
    assert literals == [text]


@pytest.mark.parametrize("text", HOSTILE_TEXT)
def test_a_listed_operand_never_leaves_its_quotes(text):
    sql = _compile({"field": "host", "operator": "in", "value": [text, "b"]})

    statement = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {sql}", read="clickhouse")
    literals = [lit.this for lit in statement.find_all(sqlglot.exp.Literal) if lit.is_string]
    assert literals == [text, "b"]


SHAPE_REFUSED = {
    "an unknown key such as not": ({"field": "a", "not": True, "value": "x"}, "not"),
    "always, which a rule never takes": ({"field": "a", "operator": "always"}, "operator"),
    "an operator no test has": ({"field": "a", "operator": "regex", "value": "x"}, "operator"),
    "a field with a space": ({"field": "a b", "value": "x"}, "not a column or a dotted path"),
    "a field with a backtick": ({"field": "a`b", "value": "x"}, "not a column or a dotted path"),
    "a field with an empty part": (
        {"field": "a..b", "value": "x"},
        "not a column or a dotted path",
    ),
    "a field starting with a digit": (
        {"field": "1a", "value": "x"},
        "not a column or a dotted path",
    ),
    "a field too deep": ({"field": ".".join("a" * 17), "value": "x"}, "at most 16 parts"),
    "a test with no field": ({"operator": "equals", "value": "x"}, "tests a field"),
    "a test with no value": ({"field": "a", "operator": "equals"}, "value is required"),
    "an empty text to look for": (
        {"field": "a", "operator": "includes", "value": ""},
        "matches every value",
    ),
    "exists with a value": ({"field": "a", "operator": "exists", "value": "x"}, "takes no value"),
    "includes with a number": ({"field": "a", "operator": "includes", "value": 5}, "takes text"),
    "a comparison against text that is no date": (
        {"field": "a", "operator": "gt", "value": "yesterday"},
        "ISO 8601",
    ),
    "a comparison against a boolean": ({"field": "a", "operator": "lt", "value": True}, None),
    "equals against a list": (
        {"field": "a", "operator": "equals", "value": ["a"]},
        "takes one value",
    ),
    "in against a scalar": ({"field": "a", "operator": "in", "value": "a"}, "takes a list"),
    "in against an empty list": ({"field": "a", "operator": "in", "value": []}, "at least one"),
    "in against texts and numbers": (
        {"field": "a", "operator": "in", "value": ["a", 1]},
        "not both",
    ),
    "in against too many values": (
        {"field": "a", "operator": "in", "value": list(range(MAX_LIST_VALUES + 1))},
        "at most 500",
    ),
    "a number that is not finite": (
        {"field": "a", "operator": "gt", "value": float("inf")},
        "finite",
    ),
    "an integer past 64 bits": (
        {"field": "a", "operator": "gt", "value": 2**64},
        "fits in 64 bits",
    ),
    "a text with a NUL": ({"field": "a", "value": "a\x00b"}, "NUL"),
    "a text carrying a runner placeholder": (
        {"field": "a", "value": "x{window}y"},
        "{window}, which the runner fills in",
    ),
    "a text carrying the retired time placeholder": (
        {"field": "a", "value": "{timestamp_condition}"},
        "{timestamp_condition}",
    ),
    "a text too long": ({"field": "a", "value": "a" * 1025}, "at most 1024"),
    "all and any together": (
        {"all": [{"field": "a", "value": "x"}], "any": [{"field": "b", "value": "y"}]},
        "not both",
    ),
    "a group that also tests a field": (
        {"all": [{"field": "a", "value": "x"}], "field": "b", "operator": "equals", "value": "y"},
        "do not apply beside all or any",
    ),
    "an empty group": ({"all": []}, "tests a field"),
    "groups past the nesting limit": (
        None,
        f"at most {MATCH_NESTING_LIMIT} groups deep",
    ),
    "more tests than the limit": (
        {"all": [{"field": "a", "value": "x"}] * (MAX_TESTS + 1)},
        f"at most {MAX_TESTS} tests",
    ),
}


def _nested(depth: int) -> dict:
    body: dict = {"field": "a", "value": "x"}
    for _ in range(depth):
        body = {"all": [body]}
    return body


@pytest.mark.parametrize(("body", "message"), SHAPE_REFUSED.values(), ids=list(SHAPE_REFUSED))
def test_a_condition_the_builder_cannot_write_is_refused(body, message):
    if body is None:
        body = _nested(MATCH_NESTING_LIMIT + 1)

    with pytest.raises(ValidationError) as refused:
        RuleCondition.model_validate(body)

    if message is not None:
        assert message in str(refused.value)


def test_groups_at_the_nesting_limit_are_accepted():
    assert RuleCondition.model_validate(_nested(MATCH_NESTING_LIMIT)).depth() == MATCH_NESTING_LIMIT


def test_a_test_with_no_operator_is_an_equals():
    assert RuleCondition.model_validate({"field": "a", "value": "x"}).operator == "equals"


def test_every_operator_names_the_kind_of_value_it_takes():
    """The published list is the operators the model accepts, no more and no fewer."""
    schema = RuleCondition.model_json_schema()["$defs"]["RuleCondition"]["properties"]["operator"]
    accepted = next(branch["enum"] for branch in schema["anyOf"] if "enum" in branch)

    assert set(accepted) == set(OPERAND_KINDS)
    assert schema["x-dfe-operand-kinds"] == OPERAND_KINDS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-31", "toDateTime64('2026-01-31 00:00:00.000', 3, 'UTC')"),
        ("2026-01-31T09:30:15.123456", "toDateTime64('2026-01-31 09:30:15.123', 3, 'UTC')"),
        ("2026-01-31T09:30:00Z", "toDateTime64('2026-01-31 09:30:00.000', 3, 'UTC')"),
        ("2026-01-31T09:30:00-05:00", "toDateTime64('2026-01-31 14:30:00.000', 3, 'UTC')"),
    ],
)
def test_an_instant_is_read_as_utc(value, expected):
    assert instant_literal(value) == expected


@pytest.mark.parametrize("value", ["", "now()", "2026-13-01", "1 OR 1=1", "2026-01-01'); --"])
def test_an_instant_that_is_not_iso_8601_is_refused(value):
    with pytest.raises(ValueError, match="ISO 8601"):
        instant_literal(value)


# The one compiled case the rule reader changes: it takes a time bound out of a rule's filter.
_TIME_BOUND = "a column against a date-time with an offset is moved to UTC"
READ_BACK = {name: case for name, case in COMPILED.items() if name != _TIME_BOUND}


@pytest.mark.parametrize(("body", "sql"), READ_BACK.values(), ids=list(READ_BACK))
def test_reading_the_compiled_sql_as_a_rule_gives_the_same_where(body, sql):
    """What the rule stores is what the condition says, so a save cannot widen it."""
    where = _compile(body)

    parsed = RuleRewriter().parse_user_sql(f"SELECT * FROM `dfe`.`cisco-ios` WHERE {where}")

    assert parsed.where_clause == where
    assert (parsed.source_db, parsed.source_table) == ("dfe", "cisco-ios")


def test_the_one_case_the_rule_reader_changes_is_a_time_bound():
    where = _compile(COMPILED[_TIME_BOUND][0])

    parsed = RuleRewriter().parse_user_sql(f"SELECT * FROM `dfe`.`cisco-ios` WHERE {where}")

    assert parsed.where_clause == ""
    assert parsed.stripped_time_bounds
