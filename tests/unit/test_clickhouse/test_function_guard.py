#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_function_guard.py
#  Purpose:      Caller SQL may not call a function that reads outside the row or calls out
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The one checker every caller-SQL path runs.

The sampler filter, a detection rule's condition and a schema column's default
all reach ClickHouse as text the engine composed, so each is held to the same
list. The addresses below are in the documentation range (RFC 5737) and nothing
here opens a socket.
"""

import pytest
from sigma.collection import SigmaCollection

from dfe_engine.clickhouse.function_guard import (
    CallNotPermittedError,
    refuse_calls_outside_the_row,
)
from dfe_engine.sampling import clickhouse_reader
from dfe_engine.sigma.sigma_backend_clickhouse import SqlBackend

# One call per family that leaves the row, and what the refusal has to say about it.
REFUSED_CALLS = {
    "url": ("url('http://203.0.113.9/leak', 'LineAsString') = 1", "reads outside the table"),
    "s3": ("s3('https://203.0.113.9/bucket/key', 'CSV') = 1", "reads outside the table"),
    "file": ("file('hostname') LIKE '%a%'", "reads outside the table"),
    "remote": ("remote('203.0.113.9', system.users) = 1", "reads outside the table"),
    "dictionary": ("dictGet('tenants', 'name', toUInt64(1)) = 'acme'", "reads outside the table"),
    "embedded dictionary": ("regionToName(toUInt32(1)) = 'x'", "reads outside the table"),
    "join table": ("joinGet('j', 'v', 1) = 'x'", "reads outside the table"),
    "ai": ("aiFilter(message, 'is it bad') = 1", "sends data to another service"),
    "keeper counter": ("generateSerialID(message) > 0", "sends data to another service"),
    "globalIn": ("globalIn(severity, dfe.main)", "can read a table"),
    "notIn": ("notIn(severity, dfe.main)", "can read a table"),
    "in": ("in(severity, dfe.main)", "can read a table"),
}

# Where an operand starts, so ClickHouse reads in(...) as the call, not the IN operator.
IN_CALL_POSITIONS = {
    "first": "in(severity, dfe.main)",
    "upper case": "IN(severity, dfe.main)",
    "after AND": "severity = 'high' AND in(severity, dfe.main)",
    "after NOT": "NOT in(severity, dfe.main)",
    "after AND NOT": "severity = 'high' AND NOT in(severity, dfe.main)",
    "in brackets": "(in(severity, dfe.main))",
    "as an argument": "toUInt8(in(severity, dfe.main)) = 1",
    "after a comma": "if(1, in(severity, dfe.main), 0)",
    "in a CASE": "CASE WHEN in(severity, dfe.main) THEN 1 ELSE 0 END = 1",
    "as the CASE operand": "CASE in(severity, dfe.main) WHEN 1 THEN 1 END = 1",
    "as an INTERVAL amount": "_timestamp > now() - INTERVAL in(1, dfe.main) DAY",
    "after LIKE": "severity LIKE in(severity, dfe.main)",
    "after DIV": "1 DIV in(1, dfe.main) = 0",
    "after a comparison": "1 = in(severity, dfe.main)",
    "in a lambda": "arrayExists(z -> in(z, dfe.main), [severity])",
    "as the IN operator's list": "severity IN in(severity, dfe.main)",
    "in a whole rule": "SELECT * FROM dfe.main WHERE in(severity, dfe.main)",
}

# The IN operator: a lookup against a table or subquery is a rule's to make.
IN_OPERATOR_CONDITIONS = [
    "severity IN (SELECT ioc FROM threat.iocs)",
    "severity NOT IN (SELECT ioc FROM threat.iocs)",
    "severity GLOBAL IN (SELECT ioc FROM threat.iocs)",
    "severity GLOBAL NOT IN (SELECT ioc FROM threat.iocs)",
    "severity IN threat.iocs",
    "severity IN ('high', 'critical')",
    "severity IN('high')",
    "lower(severity) IN ('high')",
    "tuple(a, b) IN ((1, 2))",
    "CASE WHEN a = 1 THEN 'x' ELSE 'y' END IN ('x')",
    "`field name` IN ('a')",
    "date IN ('2026-10-03')",
    "DestinationIp in ('10.0.0.0/8')",
    "SELECT * FROM dfe.main WHERE severity IN (SELECT ioc FROM threat.iocs)",
]

# Conditions the shipped paths produce: the docker stack's seeded rule, a sigma
# backend condition, and the shapes a hand-written rule uses.
PERMITTED_CONDITIONS = [
    "toString(`_tags`.marker) = 'run-1'",
    "JSONExtractString(toString(`_tags`), 'marker') = 'run-1'",
    "toString(_json.event_type) = 'login_failure'",
    "EventID = '1' AND Image LIKE '%\\\\certutil.exe'",
    "severity IN ('high', 'critical')",
    "process_name = 'certutil.exe' AND NOT startsWith(host_name, 'test-')",
    "arrayExists(z -> z IN ('root', 'admin'), [user_name])",
    "_timestamp_load >= now64(3) - INTERVAL 5 MINUTE",
    "length(message) > 0 AND match(message, 'failed')",
]


@pytest.mark.parametrize(("sql", "reason"), REFUSED_CALLS.values(), ids=list(REFUSED_CALLS))
def test_a_call_that_leaves_the_row_is_refused_and_says_why(sql, reason):
    with pytest.raises(CallNotPermittedError) as caught:
        refuse_calls_outside_the_row(sql, subject="A rule's detection condition")

    assert reason in str(caught.value)
    assert str(caught.value).startswith("A rule's detection condition may not call ")


@pytest.mark.parametrize("sql", PERMITTED_CONDITIONS)
def test_a_condition_over_the_row_passes(sql):
    refuse_calls_outside_the_row(sql, subject="A rule's detection condition")


@pytest.mark.parametrize("sql", IN_CALL_POSITIONS.values(), ids=list(IN_CALL_POSITIONS))
def test_the_in_call_is_refused_wherever_an_operand_starts(sql):
    with pytest.raises(CallNotPermittedError) as caught:
        refuse_calls_outside_the_row(sql, subject="A rule's SQL")

    assert "may not call in(), which can read a table" in str(caught.value).lower()


@pytest.mark.parametrize("sql", IN_OPERATOR_CONDITIONS)
def test_the_in_operator_is_left_alone(sql):
    refuse_calls_outside_the_row(sql, subject="A rule's SQL")


def test_sigma_backend_output_passes():
    rule = SigmaCollection.from_yaml(
        """
        title: Certutil decode
        status: test
        logsource:
            category: process_creation
            product: windows
        detection:
            selection:
                EventID:
                    - 4688
                    - 1
                Image|endswith: '\\\\certutil.exe'
                DestinationIp|cidr:
                    - '10.0.0.0/8'
            condition: selection
        """
    )
    [condition] = SqlBackend().convert(rule)

    refuse_calls_outside_the_row(condition, subject="A rule's detection condition")


def test_the_subject_names_what_the_caller_supplied():
    with pytest.raises(CallNotPermittedError) as caught:
        refuse_calls_outside_the_row("url('http://203.0.113.9/')", subject="The default for 'host'")

    assert str(caught.value) == (
        "The default for 'host' may not call url(), which reads outside the table"
    )


def test_a_quoted_call_name_is_refused_too():
    with pytest.raises(CallNotPermittedError, match="reads outside the table"):
        refuse_calls_outside_the_row("`url`('http://203.0.113.9/') = 1", subject="filter")


def test_sql_that_cannot_be_tokenized_is_refused_rather_than_passed():
    with pytest.raises(CallNotPermittedError, match="is not ClickHouse SQL"):
        refuse_calls_outside_the_row("level = 'unterminated", subject="filter")


def test_the_sampler_runs_this_checker_rather_than_its_own_copy():
    # One implementation: the sampler's refusal is this module's, worded for a filter.
    with pytest.raises(CallNotPermittedError) as caught:
        clickhouse_reader.filter_predicate("url('http://203.0.113.9/', 'LineAsString') = 1")

    assert str(caught.value) == "filter may not call url(), which reads outside the table"
