"""Integration tests for alert grouping + per-group cooldown against ClickHouse.

Requires a running ClickHouse instance (Docker or DevEx cluster).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.hunts.alert_grouping import (
    AlertStateManager,
    build_group_key,
    build_grouping_query,
)
from dfe_engine.settings import get_settings

pytestmark = pytest.mark.integration


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def ch_client():
    settings = get_settings()
    config = {
        "ch_host": settings.clickhouse.host,
        "ch_port": settings.clickhouse.port,
        "ch_username": settings.clickhouse.username,
        "ch_password": settings.clickhouse.password,
        "ch_secure": settings.clickhouse.secure,
        "ch_verify": settings.clickhouse.verify,
    }
    client = ClickHouseManager.get_instance(target_config_data=config).get_clickhouse_client()
    return client


@pytest.fixture(scope="module")
def test_db(ch_client):
    db_name = f"dfe_test_alertgrp_{uuid.uuid4().hex[:8]}"
    ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {db_name}")
    yield db_name
    ch_client.execute(f"DROP DATABASE IF EXISTS {db_name}")


@pytest.fixture(scope="module")
def results_table(ch_client, test_db):
    """Create a hunt results table and seed test data."""
    table = "detection"
    ch_client.execute(f"""
        CREATE TABLE {test_db}.{table} (
            _timestamp      DateTime64(3, 'UTC'),
            _timestamp_load DateTime64(3, 'UTC') DEFAULT now64(3),
            _org_id         LowCardinality(String),
            _uuid           UUID DEFAULT generateUUIDv7(),
            _source         LowCardinality(String) DEFAULT 'test',
            _json           String DEFAULT '',
            matched_uuid    UUID DEFAULT generateUUIDv7(),
            rule_id         LowCardinality(String),
            rule_name       LowCardinality(String),
            source_table    LowCardinality(String) DEFAULT 'test_source',
            hunt_name       LowCardinality(String),
            severity        LowCardinality(String)
        ) ENGINE = MergeTree()
        ORDER BY (_timestamp_load, _timestamp, _org_id)
    """)

    # Seed 50 rows across 3 severities and 4 source_ip values
    base_ts = datetime(2026, 3, 3, 10, 0, 0, tzinfo=UTC)
    rows = []
    source_ips = ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]
    severities = ["high", "medium", "low"]
    # Distribution: high=20, medium=18, low=12 (total 50)
    # Per source_ip: 10.0.0.1=15, 10.0.0.2=15, 10.0.0.3=10, 10.0.0.4=10
    for i in range(50):
        sev = severities[i % 3] if i < 48 else "high"  # high gets +2 extra
        ip = source_ips[i % 4]
        ts = (base_ts + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S.000")
        json_str = f'{{"source_ip": "{ip}", "index": {i}}}'
        rows.append(
            f"('{ts}', 'acme', 'test_rule_1', 'priv_esc', 'test_source', "
            f"'hunt_alpha', '{sev}', '{json_str}')"
        )

    values = ", ".join(rows)
    ch_client.execute(
        f"INSERT INTO {test_db}.{table} "
        f"(_timestamp, _org_id, rule_id, rule_name, source_table, hunt_name, severity, _json) "
        f"VALUES {values}"
    )
    return table


@pytest.fixture
def alert_state_db(ch_client, test_db):
    """Provide an AlertStateManager using the test database."""
    mgr = AlertStateManager(database=test_db)
    mgr.ensure_table_exists(ch_client)
    return mgr


RESULTS_COLS = frozenset(
    {
        "_timestamp",
        "_timestamp_load",
        "_org_id",
        "_uuid",
        "_source",
        "matched_uuid",
        "rule_id",
        "rule_name",
        "source_table",
        "hunt_name",
        "severity",
        "_json",
    }
)


# ── Grouping Query Tests ───────────────────────────────────────


class TestGroupingQuery:
    def test_group_by_direct_column(self, ch_client, test_db, results_table):
        """GROUP BY severity produces one row per severity with correct counts."""
        sql = build_grouping_query(
            target_db=test_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["severity"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_COLS,
        )
        rows = ch_client.execute(sql)

        assert len(rows) == 3  # high, medium, low
        # Each row: (severity, match_count, first_seen, last_seen, sample_events)
        total = sum(r[1] for r in rows)
        assert total == 50

    def test_group_by_json_field(self, ch_client, test_db, results_table):
        """GROUP BY source_ip extracts from _json via JSONExtractString."""
        sql = build_grouping_query(
            target_db=test_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["source_ip"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_COLS,
        )
        rows = ch_client.execute(sql)

        assert len(rows) == 4  # 4 distinct IPs
        ips = {r[0] for r in rows}
        assert ips == {"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"}
        total = sum(r[1] for r in rows)
        assert total == 50

    def test_group_by_mixed_fields(self, ch_client, test_db, results_table):
        """GROUP BY severity + source_ip produces combined groups."""
        sql = build_grouping_query(
            target_db=test_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["severity", "source_ip"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_COLS,
        )
        rows = ch_client.execute(sql)

        # 3 severities x 4 IPs = up to 12 groups
        assert len(rows) >= 3  # at least one per severity
        assert len(rows) <= 12
        total = sum(r[2] for r in rows)  # match_count is 3rd column (sev, ip, count, ...)
        assert total == 50

    def test_aggregation_columns(self, ch_client, test_db, results_table):
        """Verify match_count, first_seen, last_seen, sample_events columns."""
        sql = build_grouping_query(
            target_db=test_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["severity"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_COLS,
        )
        rows = ch_client.execute(sql)

        for row in rows:
            severity, match_count, first_seen, last_seen, sample_events = row
            assert match_count > 0
            assert first_seen <= last_seen
            assert len(sample_events) > 0
            assert len(sample_events) <= 10  # default max_sample_events

    def test_max_sample_events_cap(self, ch_client, test_db, results_table):
        """groupArray(N) caps sample_events at N."""
        sql = build_grouping_query(
            target_db=test_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["severity"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_COLS,
            max_sample_events=3,
        )
        rows = ch_client.execute(sql)

        for row in rows:
            sample_events = row[4]
            assert len(sample_events) <= 3


# ── Alert State Cooldown Tests ─────────────────────────────────


class TestAlertStateCooldown:
    def test_cooldown_lifecycle(self, ch_client, alert_state_db):
        """record fire → check within window (blocked) → check after window (allowed)."""
        mgr = alert_state_db
        hunt = f"hunt_{uuid.uuid4().hex[:6]}"

        # No prior fire — should be allowed
        assert mgr.check_cooldown(ch_client, hunt, "rule_1", "acme", timedelta(hours=1)) is True

        # Record fire
        mgr.record_fire(ch_client, hunt, "rule_1", "acme")

        # Check within window — should be blocked
        assert mgr.check_cooldown(ch_client, hunt, "rule_1", "acme", timedelta(hours=1)) is False

        # Check with zero cooldown — always allowed
        assert mgr.check_cooldown(ch_client, hunt, "rule_1", "acme", timedelta(0)) is True

    def test_per_group_cooldown(self, ch_client, alert_state_db):
        """Fire group A → group B still fires, group A blocked."""
        mgr = alert_state_db
        hunt = f"hunt_{uuid.uuid4().hex[:6]}"
        group_a = build_group_key(["source_ip"], {"source_ip": "10.0.0.1"})
        group_b = build_group_key(["source_ip"], {"source_ip": "10.0.0.2"})

        # Fire group A
        mgr.record_fire(ch_client, hunt, "rule_1", "acme", group_key=group_a)

        # Group A blocked, group B still allowed
        assert (
            mgr.check_cooldown(
                ch_client, hunt, "rule_1", "acme", timedelta(hours=1), group_key=group_a
            )
            is False
        )
        assert (
            mgr.check_cooldown(
                ch_client, hunt, "rule_1", "acme", timedelta(hours=1), group_key=group_b
            )
            is True
        )

        # Fire group B
        mgr.record_fire(ch_client, hunt, "rule_1", "acme", group_key=group_b)

        # Both now blocked
        assert (
            mgr.check_cooldown(
                ch_client, hunt, "rule_1", "acme", timedelta(hours=1), group_key=group_a
            )
            is False
        )
        assert (
            mgr.check_cooldown(
                ch_client, hunt, "rule_1", "acme", timedelta(hours=1), group_key=group_b
            )
            is False
        )

    def test_ensure_table_idempotent(self, ch_client, test_db):
        """Two ensure_table calls don't error."""
        mgr = AlertStateManager(database=test_db)
        mgr.ensure_table_exists(ch_client)
        mgr._table_ensured = False  # Force second attempt
        mgr.ensure_table_exists(ch_client)
