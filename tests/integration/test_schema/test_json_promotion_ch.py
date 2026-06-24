#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_json_promotion_ch.py
#  Purpose:      Integration tests for JSON path discovery against ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Integration tests for ``discover_paths`` against a real ClickHouse.

Exercises the actual JSONDynamicPathsWithTypes / _json subcolumn / uniqHLL12
SQL the service emits -- the database-dependent half of the JSON promotion
feature that unit tests deliberately leave to integration.
"""

from __future__ import annotations

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseClientWrapper
from dfe_engine.services.schema.json_promotion_service import discover_paths, sample_rows

pytestmark = pytest.mark.integration

TABLE = "json_promotion_it"
ROWS = [
    '{"user": {"email": "a@b.com", "score": 1}}',
    '{"user": {"email": "c@d.com", "score": 2}}',
    '{"user": {"email": "e@f.com", "score": 3, "mixed": "text"}}',
    '{"user": {"email": "g@h.com"}}',
    '{"user": {"mixed": 5}}',
]


@pytest.fixture
def seeded_table(clickhouse_client, clickhouse_test_database):
    """Create + populate a table with a JSON ``_json`` column, or skip."""
    db = clickhouse_test_database
    try:
        clickhouse_client.command("SET allow_experimental_json_type = 1")
        clickhouse_client.command(
            f"CREATE TABLE {db}.{TABLE} (_json JSON) ENGINE = MergeTree ORDER BY tuple()"
        )
        for row in ROWS:
            clickhouse_client.command(
                f'INSERT INTO {db}.{TABLE} FORMAT JSONEachRow {{"_json": {row}}}'
            )
        # Probe that the discovery function is available on this CH version.
        clickhouse_client.query(
            f"SELECT JSONDynamicPathsWithTypes(_json) FROM {db}.{TABLE} LIMIT 1"
        )
    except Exception as exc:
        pytest.skip(f"ClickHouse JSON type / dynamic paths unavailable: {exc}")
    yield db
    clickhouse_client.command(f"DROP TABLE IF EXISTS {db}.{TABLE}")


def _by_path(seeded_table, clickhouse_client, **kwargs):
    wrapped = ClickHouseClientWrapper(clickhouse_client)
    discovered = discover_paths(
        wrapped, db=seeded_table, source=TABLE, existing_columns=[], **kwargs
    )
    return {d.path: d for d in discovered}


class TestDiscoverPathsIntegration:
    def test_discovers_paths_and_types(self, seeded_table, clickhouse_client):
        by_path = _by_path(seeded_table, clickhouse_client)
        assert "user.email" in by_path
        assert by_path["user.email"].types == ["String"]
        assert by_path["user.email"].is_consistent is True

    def test_inconsistent_path_flips(self, seeded_table, clickhouse_client):
        by_path = _by_path(seeded_table, clickhouse_client)
        mixed = by_path["user.mixed"]
        assert mixed.is_consistent is False
        assert len(mixed.types) >= 2

    def test_samples_are_distinct(self, seeded_table, clickhouse_client):
        by_path = _by_path(seeded_table, clickhouse_client, samples=3)
        samples = by_path["user.email"].samples
        assert samples is not None
        assert len(samples) == len(set(samples))
        assert len(samples) <= 3

    def test_stats_are_sane(self, seeded_table, clickhouse_client):
        by_path = _by_path(seeded_table, clickhouse_client, stats=True)
        email = by_path["user.email"]
        assert email.coverage_pct is not None
        assert 0.0 < email.coverage_pct <= 100.0
        assert email.distinct_count is not None
        assert email.distinct_count >= 3

    def test_paths_filter_narrows(self, seeded_table, clickhouse_client):
        by_path = _by_path(seeded_table, clickhouse_client, paths=["user.email"])
        assert set(by_path) == {"user.email"}


class TestSampleRowsIntegration:
    def test_samples_whole_table(self, seeded_table, clickhouse_client):
        wrapped = ClickHouseClientWrapper(clickhouse_client)
        columns, rows = sample_rows(wrapped, db=seeded_table, source=TABLE, limit=3)
        assert columns == ["_json"]
        assert 1 <= len(rows) <= 3
        assert all("_json" in row for row in rows)

    def test_limit_caps_row_count(self, seeded_table, clickhouse_client):
        wrapped = ClickHouseClientWrapper(clickhouse_client)
        _columns, rows = sample_rows(wrapped, db=seeded_table, source=TABLE, limit=2)
        assert len(rows) == 2
