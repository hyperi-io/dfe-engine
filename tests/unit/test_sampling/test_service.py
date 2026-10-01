#  Project:      dfe-engine
#  File:         tests/unit/test_sampling/test_service.py
#  Purpose:      Unit tests for the Sampler service (mode dispatch, resolution)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Sampler service unit tests.

Fast modes (recent/random) run against a fake ClickHouse client. Gated modes
(smart/anomaly) are exercised via their graceful degradation - logreducer is not
installed in this env, which is exactly the pre-PyPI state we ship for.
"""

from __future__ import annotations

import asyncio

import pytest

from dfe_engine.sampling import (
    SampleBackend,
    SampleMode,
    Sampler,
    SampleRequest,
    SamplerError,
)
from dfe_engine.sampling.service import _reservoir
from dfe_engine.settings import ClickHouseSettings, KafkaSettings, SamplerSettings
from dfe_engine.source.registry import SourceNotFoundError


class _Result:
    def __init__(self, rows: list[str]) -> None:
        self.result_rows = [[r] for r in rows]


class FakeCH:
    """Minimal stand-in for ClickHouseClientWrapper.query()."""

    def __init__(self, rows: list[str]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict, dict]] = []

    def query(self, sql, parameters=None, settings=None):
        self.calls.append((sql, parameters or {}, settings or {}))
        return _Result(self._rows)


class _FakeSource:
    table_name = "events"
    topic_land = "events_land"


class FakeRegistry:
    def __init__(self, present: bool = True) -> None:
        self._present = present

    def get_source(self, name: str):
        if not self._present:
            raise SourceNotFoundError(f"missing: {name}")
        return _FakeSource()


def _sampler() -> Sampler:
    return Sampler(SamplerSettings(), KafkaSettings(), ClickHouseSettings(data_database="dfe_data"))


def _run(req, ch, reg):
    return asyncio.run(_sampler().run(req, ch, reg))


# ── fast modes ─────────────────────────────────────────────────


def test_recent_clickhouse_parses_rows_and_keys():
    ch = FakeCH(['{"a": 1, "b": "x"}', '{"a": 2, "c": true}'])
    req = SampleRequest(mode=SampleMode.RECENT, table="`db`.`events`", limit=5)
    out = _run(req, ch, None)

    assert out["mode"] == "recent"
    assert out["count"] == 2
    assert out["rows"] == [{"a": 1, "b": "x"}, {"a": 2, "c": True}]
    assert out["keys"] == ["a", "b", "c"]
    # Ordered by the configured timestamp field, DESC.
    assert "ORDER BY timestamp_load DESC" in ch.calls[0][0]


def test_random_clickhouse_uses_rand_with_seed():
    ch = FakeCH(['{"a": 1}'])
    req = SampleRequest(mode=SampleMode.RANDOM, table="`db`.`events`", seed=42)
    _run(req, ch, None)
    assert "rand(42)" in ch.calls[0][0]


def test_filter_and_source_label_go_into_where():
    ch = FakeCH([])
    # explicit table + source name -> _source filter is applied
    req = SampleRequest(
        mode=SampleMode.RECENT,
        table="`db`.landing",
        source="filebeat",
        filter="status = 500",
    )
    _run(req, ch, None)
    sql, params, _ = ch.calls[0]
    assert "_source = {src:String}" in sql
    assert params["src"] == "filebeat"
    assert "(status = 500)" in sql


# ── target resolution ──────────────────────────────────────────


def test_registered_source_resolves_to_qualified_table():
    ch = FakeCH([])
    req = SampleRequest(mode=SampleMode.RECENT, source="filebeat")
    _run(req, ch, FakeRegistry())
    assert "`dfe_data`.`events`" in ch.calls[0][0]


def test_explicit_table_is_requoted():
    ch = FakeCH([])
    _run(SampleRequest(mode=SampleMode.RECENT, table="`db`.landing"), ch, None)
    assert "FROM `db`.`landing` " in ch.calls[0][0]


@pytest.mark.parametrize(
    "table", ["(SELECT name FROM system.users)", "db.events WHERE 1=1", "`db`.`ev`ents`"]
)
def test_explicit_table_that_is_not_a_table_name_is_refused(table):
    req = SampleRequest(mode=SampleMode.RECENT, table=table)
    with pytest.raises(SamplerError, match="not a table reference"):
        _sampler().resolve_or_raise(req, None)


def test_clickhouse_without_source_or_table_raises():
    with pytest.raises(SamplerError):
        _sampler().resolve_or_raise(SampleRequest(mode=SampleMode.RECENT), None)


def test_missing_source_raises_samplererror():
    req = SampleRequest(mode=SampleMode.RECENT, source="nope")
    with pytest.raises(SamplerError):
        _sampler().resolve_or_raise(req, FakeRegistry(present=False))


def test_kafka_target_is_land_topic():
    assert (
        _sampler().resolve_or_raise(
            SampleRequest(backend=SampleBackend.KAFKA, mode=SampleMode.RECENT, source="filebeat"),
            FakeRegistry(),
        )
        == "events_land"
    )


# ── gated modes without logreducer ─────────────────────────────


def test_smart_without_logreducer_raises_clean_error():
    ch = FakeCH(['{"a": 1}'])
    req = SampleRequest(mode=SampleMode.SMART, table="`db`.`events`")
    with pytest.raises(SamplerError, match="logreducer"):
        _run(req, ch, None)


# ── helpers ────────────────────────────────────────────────────


def test_reservoir_is_seeded_and_deterministic():
    items = [str(i) for i in range(100)]
    a = _reservoir(items, 5, 7)
    b = _reservoir(items, 5, 7)
    assert a == b
    assert len(a) == 5


def test_reservoir_returns_all_when_small():
    assert _reservoir(["a", "b"], 10, None) == ["a", "b"]
