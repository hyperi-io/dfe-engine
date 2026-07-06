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


def test_random_clickhouse_seed_is_deterministic_hash():
    # CH rand(x) ignores x, so a seed must key a deterministic hash ordering,
    # not rand(seed) (which is not reproducible).
    ch = FakeCH(['{"a": 1}'])
    req = SampleRequest(mode=SampleMode.RANDOM, table="`db`.`events`", seed=42)
    _run(req, ch, None)
    sql = ch.calls[0][0]
    assert "cityHash64(toString(_json), 42)" in sql
    assert "rand(42)" not in sql


def test_random_clickhouse_unseeded_uses_rand():
    ch = FakeCH(['{"a": 1}'])
    req = SampleRequest(mode=SampleMode.RANDOM, table="`db`.`events`")
    _run(req, ch, None)
    assert "ORDER BY rand()" in ch.calls[0][0]


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


def test_clickhouse_without_source_or_table_raises():
    with pytest.raises(SamplerError):
        _sampler().resolve_or_raise(SampleRequest(mode=SampleMode.RECENT), None)


def test_missing_source_raises_samplererror():
    req = SampleRequest(mode=SampleMode.RECENT, source="nope")
    with pytest.raises(SamplerError):
        _sampler().resolve_or_raise(req, FakeRegistry(present=False))


@pytest.mark.parametrize(
    "bad",
    [
        "(SELECT toString((*,)) AS _json FROM dfe_internal.repository) AS t",
        "db.events; DROP TABLE x",
        "db.events WHERE 1=1",
        "a b",
        "db.'events'",
        "db.events, other",
    ],
)
def test_explicit_table_override_must_be_bare_identifier(bad):
    # An explicit `table` is interpolated into FROM {target}; a subselect or any
    # non-identifier must be refused, not run on the admin client (F-SAMPLER-SQLI).
    with pytest.raises(SamplerError, match="Invalid table"):
        _sampler().resolve_or_raise(SampleRequest(mode=SampleMode.RECENT, table=bad), None)


def test_plain_and_backtick_table_overrides_pass():
    s = _sampler()
    # A bare db.table and a backtick-quoted one are both valid identifiers.
    assert (
        s.resolve_or_raise(SampleRequest(mode=SampleMode.RECENT, table="mydb.events"), None)
        == "mydb.events"
    )
    assert (
        s.resolve_or_raise(SampleRequest(mode=SampleMode.RECENT, table="`db`.`events`"), None)
        == "`db`.`events`"
    )


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


# ── FIX 4: memory gate must stay held while the worker thread runs ──────────


def test_gate_held_until_worker_finishes_on_cancel():
    """Cancelling a gated task must NOT release the memory gate while the
    logreducer worker thread is still running (asyncio.to_thread is not
    cancellable) - else the max_concurrent x max_memory_gb ceiling is
    undercounted for the rest of that run."""
    import threading

    started = threading.Event()
    release = threading.Event()

    sampler = Sampler(
        SamplerSettings(max_concurrent=1),
        KafkaSettings(),
        ClickHouseSettings(data_database="dfe_data"),
    )

    def blocking_reduce(req, ch, target, source_label, limit):
        started.set()
        release.wait(5)
        return ["{}"], {"truncated": False}, None

    sampler._reduce_sample = blocking_reduce  # type: ignore[method-assign]

    async def scenario():
        req = SampleRequest(mode=SampleMode.SMART, table="`db`.`events`")
        task = asyncio.ensure_future(sampler.run(req, FakeCH([]), None))

        # Wait for the worker thread to start (gate has been acquired).
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        sem = sampler._semaphore()
        assert sem.locked()  # permit held

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Worker thread is still alive -> the gate MUST stay held.
        assert sem.locked()

        # Let the worker finish; its done-callback then releases the gate.
        release.set()
        for _ in range(200):
            if not sem.locked():
                break
            await asyncio.sleep(0.02)
        assert not sem.locked()

    asyncio.run(scenario())


# ── FIX 5: skewed-partition tail redistribution (fake confluent-kafka) ──────


class _FakeMsg:
    def __init__(self, partition, offset, value, err=None):
        self._p, self._o, self._v, self._e = partition, offset, value, err

    def error(self):
        return self._e

    def partition(self):
        return self._p

    def offset(self):
        return self._o

    def value(self):
        return self._v


class _FakeKafkaError:
    _PARTITION_EOF = -191


class _FakeTP:
    def __init__(self, topic, partition, offset=-1001):
        self.topic, self.partition, self.offset = topic, partition, offset


class _FakePartMeta:
    error = None


class _FakeTopicMeta:
    def __init__(self, parts):
        self.error = None
        self.partitions = {p: _FakePartMeta() for p in parts}


class _FakeClusterMeta:
    def __init__(self, topic, parts):
        self.topics = {topic: _FakeTopicMeta(parts)}


def _make_fake_ck(layout):
    """A fake ``confluent_kafka`` module for a topic with the given
    {partition: (lo, hi)} watermark layout."""
    import types

    class _FakeConsumer:
        def __init__(self, conf):
            self._queue: list = []

        def list_topics(self, topic, timeout=None):
            return _FakeClusterMeta(topic, list(layout.keys()))

        def get_watermark_offsets(self, tp, timeout=None, cached=False):
            return layout[tp.partition]

        def assign(self, assignments):
            q: list = []
            for tp in assignments:
                _lo, hi = layout[tp.partition]
                for off in range(tp.offset, hi):
                    q.append(
                        _FakeMsg(tp.partition, off, f'{{"p":{tp.partition},"o":{off}}}'.encode())
                    )
            self._queue = q

        def poll(self, timeout=None):
            return self._queue.pop(0) if self._queue else None

        def close(self):
            pass

    mod = types.ModuleType("confluent_kafka")
    mod.Consumer = _FakeConsumer
    mod.KafkaError = _FakeKafkaError
    mod.TopicPartition = _FakeTP
    return mod


def test_read_recent_redistributes_skewed_partitions(monkeypatch):
    import sys

    from dfe_engine.sampling import kafka_reader as kr

    # 2 partitions with 1 message, 1 with 1000; limit 100.
    layout = {0: (0, 1), 1: (0, 1), 2: (0, 1000)}
    monkeypatch.setitem(sys.modules, "confluent_kafka", _make_fake_ck(layout))
    lines = kr.read_recent({}, "topic", limit=100, group_suffix="x")
    # ~100 (deep partition covers the shortfall), NOT ~34 from an even split.
    assert len(lines) == 100


def test_read_recent_returns_all_when_topic_small(monkeypatch):
    import sys

    from dfe_engine.sampling import kafka_reader as kr

    layout = {0: (0, 1), 1: (0, 1), 2: (0, 5)}
    monkeypatch.setitem(sys.modules, "confluent_kafka", _make_fake_ck(layout))
    lines = kr.read_recent({}, "topic", limit=100, group_suffix="x")
    assert len(lines) == 7  # total < limit only because the topic genuinely has fewer


def test_kafka_recent_truncated_true_when_window_filled(monkeypatch):
    # P3.12: unified 'more available' meaning - a full window means older messages
    # were left unread, so truncated is True (was inverted before).
    from dfe_engine.sampling import kafka_reader as kr

    monkeypatch.setattr(kr, "read_recent", lambda *a, **k: ["{}"] * 100)
    req = SampleRequest(backend=SampleBackend.KAFKA, mode=SampleMode.RECENT, topic="t")
    _lines, stats, _note = _sampler()._kafka_fast(req, "t", 100)
    assert stats["truncated"] is True


def test_kafka_recent_truncated_false_when_short(monkeypatch):
    # A short read cut nothing - fewer messages available than asked -> not truncated.
    from dfe_engine.sampling import kafka_reader as kr

    monkeypatch.setattr(kr, "read_recent", lambda *a, **k: ["{}"] * 7)
    req = SampleRequest(backend=SampleBackend.KAFKA, mode=SampleMode.RECENT, topic="t")
    _lines, stats, _note = _sampler()._kafka_fast(req, "t", 100)
    assert stats["truncated"] is False
