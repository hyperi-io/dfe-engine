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

from dfe_engine.clickhouse.function_guard import (
    CallNotPermittedError,
    refuse_calls_outside_the_row,
)
from dfe_engine.sampling import clickhouse_reader

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
}

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
