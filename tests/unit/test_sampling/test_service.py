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

import asyncio

import pytest
from pydantic import ValidationError

from dfe_engine.sampling import (
    SampleBackend,
    SampleMode,
    Sampler,
    SampleRequest,
    SamplerError,
)
from dfe_engine.sampling.clickhouse_reader import build_where
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


@pytest.mark.parametrize("mode", [SampleMode.RECENT, SampleMode.RANDOM])
def test_every_clickhouse_read_is_read_only(mode):
    ch = FakeCH([])
    _run(SampleRequest(mode=mode, table="`db`.`events`"), ch, None)
    [(_, _, settings)] = ch.calls
    assert settings == {"max_execution_time": SamplerSettings().max_execution_time, "readonly": 1}


# ── filter: one condition over the sampled row ─────────────────

REFUSED_FILTERS = {
    "unbalanced paren escapes the _source fence": "1) OR (1",
    "union reaches another table": "1) UNION ALL SELECT name FROM system.users WHERE (1",
    "union inside IN": "_source IN (SELECT 'a' UNION ALL SELECT 'b')",
    "scalar subquery": "(SELECT count() FROM dfe.engine_state) > 0",
    "subquery after IN": "_org_id IN (SELECT _org_id FROM dfe.events)",
    "exists": "EXISTS (SELECT 1)",
    "table after IN": "_source IN dfe.engine_state",
    "table function after IN": "x IN url('http://203.0.113.9/', 'LineAsString')",
    "url table function": "url('http://203.0.113.9/', 'LineAsString') = 1",
    "file table function": "file('secrets.txt') LIKE '%a%'",
    "remote table function": "remote('203.0.113.9', system.users) = 1",
    "dictionary read": "dictGet('tenants', 'name', toUInt64(1)) = 'acme'",
    "join table read": "joinGet('j', 'v', 1) = 'x'",
    "statement separator": "level = 'error'; DROP TABLE events",
    "trailing statement separator": "level = 'error';",
    "settings clause": "level = 'error' SETTINGS readonly = 0",
    "format clause": "level = 'error' FORMAT JSON",
    "alias renames the fenced column": "('acme' AS _source) = 'acme'",
    "query parameter": "_source = {src:String}",
    "set statement": "SET readonly = 0",
    "backslash in a column name": "`x\\\\` = 1 OR `) OR 1=1 OR (` = 1",
    "quote in a json path": (
        "JSONExtractString(toString(_json), 'a'') OR (1 = 1) OR JSONExtractString("
        "toString(_json), ''b') = 'q'"
    ),
    "escape the renderer would change": "msg = '\\x41'",
}


@pytest.mark.parametrize("filter_sql", list(REFUSED_FILTERS.values()), ids=list(REFUSED_FILTERS))
def test_a_filter_that_is_not_one_condition_is_refused(filter_sql):
    with pytest.raises(ValidationError) as caught:
        SampleRequest(mode=SampleMode.RECENT, table="`db`.`events`", filter=filter_sql)
    assert [error["loc"] for error in caught.value.errors()] == [("filter",)]


@pytest.mark.parametrize("filter_sql", list(REFUSED_FILTERS.values()), ids=list(REFUSED_FILTERS))
def test_the_where_builder_refuses_it_too(filter_sql):
    with pytest.raises(ValueError, match=r"filter|column name"):
        build_where(
            source_label="acme",
            filter_sql=filter_sql,
            since=None,
            until=None,
            timestamp_field="timestamp_load",
        )


def test_a_filter_that_skipped_request_validation_runs_no_query():
    ch = FakeCH(['{"a": 1}'])
    req = SampleRequest.model_construct(
        mode=SampleMode.RECENT, table="`db`.landing", source="acme", filter="1) OR (1"
    )
    with pytest.raises(ValueError, match="not a ClickHouse condition"):
        _run(req, ch, None)
    assert ch.calls == []


@pytest.mark.parametrize(
    ("filter_sql", "rendered"),
    [
        ("level = 'error' AND host LIKE 'web%'", "level = 'error' AND host LIKE 'web%'"),
        ("status != 500", "status <> 500"),
        ("level = 'error' /* why */", "level = 'error'"),
        ("level = 'error' -- why", "level = 'error'"),
        ("_json.level = 'error'", "_json.level = 'error'"),
        ("status IN (500, 503)", "status IN (500, 503)"),
        ("arrayExists(t -> t = 'a', tags)", "arrayExists(t -> t = 'a', tags)"),
        ("match(msg, '\\\\d+')", "match(msg, '\\\\d+')"),
        ("msg = 'a\\'b'", "msg = 'a''b'"),
        ("   ", None),
    ],
)
def test_a_condition_is_kept_as_rendered_from_its_parse(filter_sql, rendered):
    req = SampleRequest(mode=SampleMode.RECENT, table="`db`.`events`", filter=filter_sql)
    assert req.filter == rendered


def test_the_rendered_condition_is_what_reaches_the_query():
    ch = FakeCH([])
    req = SampleRequest(
        mode=SampleMode.RECENT,
        table="`db`.landing",
        source="filebeat",
        filter="level = 'error' AND host != 'db1' -- the LIMIT must survive this",
    )
    _run(req, ch, None)
    sql, _, _ = ch.calls[0]
    assert "WHERE _source = {src:String} AND (level = 'error' AND host <> 'db1') ORDER BY" in sql
    assert sql.endswith("LIMIT {lim:UInt64}")


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
