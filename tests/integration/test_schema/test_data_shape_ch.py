#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_data_shape_ch.py
#  Purpose:      Measure cardinality against a real ClickHouse, not a fake client
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Cardinality readings against a live ClickHouse.

The unit tests pin what the counts decide; this pins that the SQL runs. Two
things only a server settles: ``uniqCombined`` over a JSON subcolumn, and the
bound -- ``SAMPLE`` raises SAMPLING_NOT_SUPPORTED on every DFE table because
none declares a sampling key, so the read is bounded by an inner LIMIT plus
``max_rows_to_read`` with ``read_overflow_mode = 'break'``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from dfe_engine.services.schema.data_shape_service import measure_cardinality

pytestmark = pytest.mark.integration

# Enough rows for a `low` reading to be honest, with a ceiling this far under
# the default so the test stays a second rather than a minute.
ROWS = 500
CEILING = 20


class Rows:
    """The engine's ``query_rows`` contract over a raw driver client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def query_rows(self, query: str, parameters: dict[str, Any] | None = None):
        result = self._client.query(query, parameters=parameters or {})
        return list(result.column_names), result.result_rows


@pytest.fixture
def seeded(ch_client):
    """A table with a bounded column, an unbounded one and a JSON payload."""
    db = f"dfe_shape_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{db}`")
    ch_client.command(
        f"CREATE TABLE `{db}`.events "
        "(`_source` String, `service` String, `trace_id` String, `_json` JSON) "
        "ENGINE = MergeTree ORDER BY tuple()"
    )
    values = ", ".join(
        "('beats', 'svc-{s}', 'trace-{i}', '{{\"host\": {{\"name\": \"h-{s}\"}}}}')".format(
            s=index % 3, i=index
        )
        for index in range(ROWS)
    )
    ch_client.command(f"INSERT INTO `{db}`.events VALUES {values}")
    ch_client.command(
        f"INSERT INTO `{db}`.events VALUES "
        "('syslog', 'other', 'trace-x', '{\"host\": {\"name\": \"h-other\"}}')"
    )
    try:
        yield db
    finally:
        try:
            ch_client.command(f"DROP DATABASE IF EXISTS `{db}` SYNC")
        except Exception:
            pass


def measure(ch_client, db: str, **kwargs):
    return measure_cardinality(Rows(ch_client), db=db, table="events", ceiling=CEILING, **kwargs)


class TestAgainstRealRows:
    def test_a_bounded_column_measures_low(self, ch_client, seeded):
        reading = measure(ch_client, seeded, column="service")
        assert reading.value == "low"
        assert reading.detail["distinct_values"] == 4
        assert reading.rows == ROWS + 1

    def test_an_unbounded_column_measures_high(self, ch_client, seeded):
        reading = measure(ch_client, seeded, column="trace_id")
        assert reading.value == "high"
        assert reading.detail["distinct_values"] > CEILING

    def test_a_json_path_measures_through_the_payload_column(self, ch_client, seeded):
        reading = measure(ch_client, seeded, json_path="host.name")
        assert reading.value == "low"
        assert reading.detail["distinct_values"] == 4

    def test_a_match_rule_scopes_the_reading_to_one_source(self, ch_client, seeded):
        reading = measure(
            ch_client, seeded, column="service", match_field="_source", match_value="beats"
        )
        assert reading.rows == ROWS
        assert reading.detail["distinct_values"] == 3

    def test_a_source_with_no_rows_reads_unknown(self, ch_client, seeded):
        """No data yet is the honest default, not a confident guess."""
        reading = measure(
            ch_client, seeded, column="service", match_field="_source", match_value="nothing"
        )
        assert reading.value == "unknown"
        assert reading.rows == 0

    def test_a_small_sample_cannot_claim_low(self, ch_client, seeded):
        """Below the ceiling in rows, a low count is the sample's shape."""
        reading = measure(ch_client, seeded, column="service", sample_rows=5)
        assert reading.rows == 5
        assert reading.value == "unknown"

    def test_the_sample_bounds_what_is_read(self, ch_client, seeded):
        reading = measure(ch_client, seeded, column="trace_id", sample_rows=100)
        assert reading.rows == 100

    def test_the_reading_carries_a_date(self, ch_client, seeded):
        reading = measure(ch_client, seeded, column="service")
        assert reading.measured_at.startswith("20")
