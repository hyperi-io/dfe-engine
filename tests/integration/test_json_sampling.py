#  Project:      dfe-engine
#  File:         tests/integration/test_json_sampling.py
#  Purpose:      Sample landed _json from real ClickHouse (feeds AI authoring)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Sampling a source's landed _json against real ClickHouse (no mocks)."""

from __future__ import annotations

import uuid

import pytest

from dfe_engine.ai.sampling import discover_json_keys, sample_json_rows


@pytest.fixture
def sample_table(ch_client):
    db = f"dfe_ai_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{db}`")
    ch_client.command(
        f"CREATE TABLE `{db}`.events (_json String, _source String) "
        "ENGINE = MergeTree ORDER BY tuple()"
    )
    ch_client.command(
        f"INSERT INTO `{db}`.events (_json, _source) VALUES "
        """('{"a":1,"b":"x"}','s1'),('{"a":2,"c":true}','s1'),('{"z":9}','s2')"""
    )
    try:
        yield f"`{db}`.events"
    finally:
        try:
            ch_client.command(f"DROP DATABASE IF EXISTS `{db}`")
        except Exception:
            pass


def test_sample_rows_filtered_by_source(ch_client, sample_table):
    rows = sample_json_rows(ch_client, sample_table, limit=10, source="s1")
    assert len(rows) == 2  # only the two s1 rows
    assert all(r.startswith("{") for r in rows)


def test_discover_keys_across_samples(ch_client, sample_table):
    rows = sample_json_rows(ch_client, sample_table, limit=10, source="s1")
    keys = discover_json_keys(rows)
    assert set(keys) == {"a", "b", "c"}  # union across the two s1 rows


def test_discover_keys_ignores_unparseable():
    assert discover_json_keys(["not json", '{"ok":1}', ""]) == ["ok"]
