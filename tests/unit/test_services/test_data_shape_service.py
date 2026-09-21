#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_data_shape_service.py
#  Purpose:      A cardinality reading is measured, bounded, and dated
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Cardinality is measured from real rows, and the reading says so.

Three rules are pinned here, because each one is a way of being confidently
wrong: ``unknown`` where the sample cannot support an answer, a reading that
carries its row count and its date rather than presenting itself as settled,
and a read bounded before it touches a 116G shard.

The SQL these tests assert is exercised against a live ClickHouse in
``tests/integration/test_schema/test_data_shape_ch.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from dfe_engine.services.schema.data_shape_service import (
    DEFAULT_SAMPLE_ROWS,
    LOW_CARDINALITY_CEILING,
    CardinalityProbe,
    DataShapeError,
    Reading,
    ShapeProbe,
    column_accessor,
    json_path_accessor,
    match_read_columns,
    measure_cardinality,
    measure_shape,
)

FIXED_NOW = datetime(2026, 9, 21, 8, 30, tzinfo=UTC)


class FakeClient:
    """Records the SQL it is handed and answers with one prepared row."""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row if row is not None else {"cardinality__distinct_values": 3, "rows": 50_000}
        self.queries: list[str] = []
        self.parameters: list[dict[str, Any]] = []

    def query_rows(self, query: str, parameters: dict[str, Any] | None = None):
        self.queries.append(query)
        self.parameters.append(dict(parameters or {}))
        return list(self.row), [tuple(self.row.values())]


class FailingClient:
    def query_rows(self, query: str, parameters: dict[str, Any] | None = None):
        raise RuntimeError("Code 60. DB::Exception: Table dfe.gone does not exist")


def measure(row: dict[str, Any], **kwargs: Any) -> Reading:
    client = FakeClient(row)
    return measure_cardinality(client, db="dfe", table="main", column="host_name", **kwargs)


# -- the three-way answer ---------------------------------------------------


class TestWhatTheCountDecides:
    def test_a_bounded_column_over_a_big_enough_sample_is_low(self):
        reading = measure({"cardinality__distinct_values": 3, "rows": 100_000})
        assert reading.value == "low"

    def test_more_distinct_values_than_the_ceiling_is_high(self):
        reading = measure({"cardinality__distinct_values": 184_203, "rows": 100_000})
        assert reading.value == "high"

    def test_high_is_proof_at_any_sample_size(self):
        """Having SEEN more than the ceiling, the column has more. Size is moot."""
        reading = measure({"cardinality__distinct_values": 10_001, "rows": 10_001})
        assert reading.value == "high"

    def test_a_sample_too_small_to_rule_out_high_stays_unknown(self):
        """3 distinct in 3 rows is the sample's shape, not the column's."""
        reading = measure({"cardinality__distinct_values": 3, "rows": 3})
        assert reading.value == "unknown"

    def test_no_data_is_unknown(self):
        """Nobody re-reviews a field that already looks decided."""
        reading = measure({"cardinality__distinct_values": 0, "rows": 0})
        assert reading.value == "unknown"

    def test_exactly_the_ceiling_over_a_big_enough_sample_is_low(self):
        reading = measure(
            {"cardinality__distinct_values": LOW_CARDINALITY_CEILING, "rows": 100_000}
        )
        assert reading.value == "low"

    def test_a_null_count_reads_as_none_seen(self):
        reading = measure({"cardinality__distinct_values": None, "rows": 0})
        assert reading.value == "unknown"


# -- a reading, not a fact --------------------------------------------------


class TestTheReadingCarriesItsProvenance:
    def test_it_carries_the_distinct_count_the_rows_and_the_date(self):
        reading = measure({"cardinality__distinct_values": 42, "rows": 100_000}, now=FIXED_NOW)
        assert reading.property == "cardinality"
        assert reading.detail["distinct_values"] == 42
        assert reading.rows == 100_000
        assert reading.measured_at == "2026-09-21T08:30:00+00:00"

    def test_it_records_the_ceiling_it_was_judged_against(self):
        """A ceiling that moves would otherwise silently reinterpret old readings."""
        reading = measure({"cardinality__distinct_values": 42, "rows": 100_000})
        assert reading.detail["ceiling"] == LOW_CARDINALITY_CEILING

    def test_a_custom_ceiling_travels_with_the_reading(self):
        reading = measure({"cardinality__distinct_values": 42, "rows": 100_000}, ceiling=10)
        assert reading.value == "high"
        assert reading.detail["ceiling"] == 10

    def test_as_dict_is_json_safe(self):
        reading = measure({"cardinality__distinct_values": 42, "rows": 100_000}, now=FIXED_NOW)
        assert reading.as_dict() == {
            "property": "cardinality",
            "value": "low",
            "rows": 100_000,
            "measured_at": "2026-09-21T08:30:00+00:00",
            "detail": {"distinct_values": 42, "ceiling": LOW_CARDINALITY_CEILING},
        }


# -- sample, do not scan ----------------------------------------------------


class TestTheReadIsBounded:
    def test_the_inner_read_is_limited(self):
        client = FakeClient()
        measure_cardinality(client, db="dfe", table="main", column="host_name")
        assert "LIMIT {sample_rows:UInt64}" in client.queries[0]
        assert client.parameters[0]["sample_rows"] == DEFAULT_SAMPLE_ROWS

    def test_the_read_carries_a_hard_cap_as_well_as_a_limit(self):
        """A selective match walks rows the LIMIT never returns; the cap stops it."""
        client = FakeClient()
        measure_cardinality(client, db="dfe", table="main", column="host_name")
        assert f"max_rows_to_read = {DEFAULT_SAMPLE_ROWS}" in client.queries[0]
        assert "read_overflow_mode = 'break'" in client.queries[0]

    def test_no_sample_clause_is_emitted(self):
        """No DFE table declares a sampling key; SAMPLE raises SAMPLING_NOT_SUPPORTED."""
        client = FakeClient()
        measure_cardinality(client, db="dfe", table="main", column="host_name")
        assert " SAMPLE " not in client.queries[0]

    def test_the_caller_can_shrink_the_sample(self):
        client = FakeClient()
        measure_cardinality(client, db="dfe", table="main", column="host_name", sample_rows=25)
        assert client.parameters[0]["sample_rows"] == 25
        assert "max_rows_to_read = 25" in client.queries[0]

    def test_a_non_positive_sample_is_refused(self):
        with pytest.raises(DataShapeError, match="positive"):
            measure_cardinality(
                FakeClient(), db="dfe", table="main", column="host_name", sample_rows=0
            )


# -- what gets read ---------------------------------------------------------


class TestTheAccessorAndTheColumnsRead:
    def test_a_column_is_read_by_name(self):
        client = FakeClient()
        measure_cardinality(client, db="dfe", table="main", column="host_name")
        assert "uniqCombined(`host_name`)" in client.queries[0]
        assert "SELECT `host_name` FROM `dfe`.`main`" in client.queries[0]

    def test_a_json_path_is_read_through_the_payload_column(self):
        client = FakeClient()
        measure_cardinality(client, db="dfe", table="main", json_path="host.name")
        assert "uniqCombined(assumeNotNull(_json).`host.name`)" in client.queries[0]
        assert "SELECT `_json` FROM `dfe`.`main`" in client.queries[0]

    def test_naming_neither_a_column_nor_a_path_is_refused(self):
        with pytest.raises(DataShapeError, match="exactly one"):
            measure_cardinality(FakeClient(), db="dfe", table="main")

    def test_naming_both_is_refused(self):
        with pytest.raises(DataShapeError, match="exactly one"):
            measure_cardinality(
                FakeClient(), db="dfe", table="main", column="c", json_path="host.name"
            )

    def test_a_backtick_in_a_path_is_refused(self):
        with pytest.raises(DataShapeError):
            measure_cardinality(FakeClient(), db="dfe", table="main", json_path="a`b")

    def test_a_header_match_column_joins_the_inner_read(self):
        client = FakeClient()
        measure_cardinality(
            client,
            db="dfe",
            table="main",
            json_path="host.name",
            match_field="_source",
            match_value="filebeat",
        )
        assert "SELECT `_json`, `_source` FROM `dfe`.`main`" in client.queries[0]
        assert client.parameters[0]["match_value"] == "filebeat"

    def test_a_payload_match_field_needs_only_the_payload_column(self):
        client = FakeClient()
        measure_cardinality(
            client,
            db="dfe",
            table="main",
            json_path="host.name",
            match_field="tags.collector.type",
            match_value="beats",
        )
        assert "SELECT `_json` FROM `dfe`.`main`" in client.queries[0]

    def test_the_match_column_is_not_read_twice(self):
        client = FakeClient()
        measure_cardinality(
            client,
            db="dfe",
            table="main",
            column="_source",
            match_field="_source",
            match_value="filebeat",
        )
        assert "SELECT `_source` FROM `dfe`.`main`" in client.queries[0]


class TestAccessorHelpers:
    def test_a_column_accessor_is_quoted(self):
        assert column_accessor("host_name") == "`host_name`"

    def test_a_json_path_accessor_matches_the_promotion_service(self):
        assert json_path_accessor("host.name") == "assumeNotNull(_json).`host.name`"

    def test_only_the_payload_column_holds_json_paths(self):
        with pytest.raises(DataShapeError, match="_json"):
            json_path_accessor("host.name", json_column="_tags")

    @pytest.mark.parametrize(
        ("match_field", "expected"),
        [
            (None, []),
            ("_source", ["_source"]),
            ("_json.tags.type", ["_json"]),
            ("tags.collector.type", ["_json"]),
        ],
    )
    def test_the_match_rule_names_the_columns_it_reads(self, match_field, expected):
        assert match_read_columns(match_field) == expected


# -- the shape the other eight properties slot into -------------------------


class NullRatioProbe(ShapeProbe):
    """A second property, to prove one bounded read serves several."""

    name = "null_ratio"

    def terms(self, accessor: str) -> dict[str, str]:
        return {"nulls": f"countIf({accessor} IS NULL)"}

    def read(self, values, rows):
        nulls = int(values["nulls"] or 0)
        return ("never_null" if nulls == 0 else "nullable"), {"nulls": nulls}


class TestSeveralProbesShareOneRead:
    def test_two_probes_produce_two_readings_from_one_query(self):
        client = FakeClient(
            {
                "cardinality__distinct_values": 4,
                "null_ratio__nulls": 0,
                "rows": 100_000,
            }
        )
        readings = measure_shape(
            client,
            db="dfe",
            table="main",
            accessor="`host_name`",
            read_columns=["host_name"],
            probes=(CardinalityProbe(), NullRatioProbe()),
        )
        assert len(client.queries) == 1
        assert [r.property for r in readings] == ["cardinality", "null_ratio"]
        assert [r.value for r in readings] == ["low", "never_null"]

    def test_each_probe_reads_only_its_own_terms(self):
        client = FakeClient(
            {
                "cardinality__distinct_values": 4,
                "null_ratio__nulls": 7,
                "rows": 100_000,
            }
        )
        readings = measure_shape(
            client,
            db="dfe",
            table="main",
            accessor="`host_name`",
            read_columns=["host_name"],
            probes=(CardinalityProbe(), NullRatioProbe()),
        )
        by_property = {r.property: r for r in readings}
        assert by_property["cardinality"].detail["distinct_values"] == 4
        assert "nulls" not in by_property["cardinality"].detail
        assert by_property["null_ratio"].detail == {"nulls": 7}

    def test_every_probe_gets_the_same_row_count_and_date(self):
        client = FakeClient(
            {
                "cardinality__distinct_values": 4,
                "null_ratio__nulls": 0,
                "rows": 12_345,
            }
        )
        readings = measure_shape(
            client,
            db="dfe",
            table="main",
            accessor="`host_name`",
            read_columns=["host_name"],
            probes=(CardinalityProbe(), NullRatioProbe()),
            now=FIXED_NOW,
        )
        assert {r.rows for r in readings} == {12_345}
        assert {r.measured_at for r in readings} == {"2026-09-21T08:30:00+00:00"}

    def test_measuring_with_no_probes_is_refused(self):
        with pytest.raises(DataShapeError, match="no probes"):
            measure_shape(
                FakeClient(),
                db="dfe",
                table="main",
                accessor="`c`",
                read_columns=["c"],
                probes=(),
            )

    def test_an_inner_read_naming_no_columns_is_refused(self):
        with pytest.raises(DataShapeError, match="no columns"):
            measure_shape(FakeClient(), db="dfe", table="main", accessor="`c`", read_columns=[])


class TestFailuresAreNamed:
    def test_a_query_failure_carries_the_server_message(self):
        with pytest.raises(DataShapeError, match="does not exist"):
            measure_cardinality(FailingClient(), db="dfe", table="gone", column="c")
