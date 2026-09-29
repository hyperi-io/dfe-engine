"""Integration tests for alert grouping + per-group cooldown against ClickHouse.

The grouping queries run over the REAL detection table and the cooldown over the REAL
alert_state table, both as the schema phase applies them, in the ``dfe_db`` database
the tiered harness provides.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from dfe_engine.hunts.alert_grouping import (
    AlertStateManager,
    build_group_key,
    build_grouping_query,
)
from dfe_engine.hunts.hunt_output import RESULTS_TABLE_COLUMNS
from dfe_engine.schema.plan import render_one

pytestmark = pytest.mark.integration


# ── Fixtures ────────────────────────────────────────────────────


def _apply_alert_state(ch_client, db: str) -> None:
    """Create alert_state in *db* from the manifest, as the schema phase does."""
    for statement in render_one("data.alert_state", data_database=db).statements:
        ch_client.command(statement)


@pytest.fixture
def results_table(manager_client, dfe_db):
    """The real detection table in ``dfe_db``, seeded with one hunt's matches."""
    table = "detection"
    test_db = dfe_db

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
    manager_client.execute(
        f"INSERT INTO {test_db}.{table} "
        f"(_timestamp, _org_id, rule_id, rule_name, source_table, hunt_name, severity, _json) "
        f"VALUES {values}"
    )
    return table


@pytest.fixture
def alert_state(ch_client, manager_client, dfe_db):
    """An AlertStateManager over ``dfe_db``, whose alert_state table the manifest created."""
    _apply_alert_state(ch_client, dfe_db)
    mgr = AlertStateManager(database=dfe_db)
    mgr.ensure_table_exists(manager_client)
    return mgr


# ── Grouping Query Tests ───────────────────────────────────────


class TestGroupingQuery:
    def test_group_by_direct_column(self, manager_client, dfe_db, results_table):
        """GROUP BY severity produces one row per severity with correct counts."""
        sql = build_grouping_query(
            target_db=dfe_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["severity"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_TABLE_COLUMNS,
        )
        rows = manager_client.execute(sql)

        assert len(rows) == 3  # high, medium, low
        # Each row: (severity, match_count, first_seen, last_seen, sample_events)
        total = sum(r[1] for r in rows)
        assert total == 50

    def test_group_by_json_field(self, manager_client, dfe_db, results_table):
        """GROUP BY source_ip extracts from _json via JSONExtractString."""
        sql = build_grouping_query(
            target_db=dfe_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["source_ip"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_TABLE_COLUMNS,
        )
        rows = manager_client.execute(sql)

        assert len(rows) == 4  # 4 distinct IPs
        ips = {r[0] for r in rows}
        assert ips == {"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"}
        total = sum(r[1] for r in rows)
        assert total == 50

    def test_group_by_mixed_fields(self, manager_client, dfe_db, results_table):
        """GROUP BY severity + source_ip produces combined groups."""
        sql = build_grouping_query(
            target_db=dfe_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["severity", "source_ip"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_TABLE_COLUMNS,
        )
        rows = manager_client.execute(sql)

        # 3 severities x 4 IPs = up to 12 groups
        assert len(rows) >= 3  # at least one per severity
        assert len(rows) <= 12
        total = sum(r[2] for r in rows)  # match_count is 3rd column (sev, ip, count, ...)
        assert total == 50

    def test_aggregation_columns(self, manager_client, dfe_db, results_table):
        """Verify match_count, first_seen, last_seen, sample_events columns."""
        sql = build_grouping_query(
            target_db=dfe_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["severity"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_TABLE_COLUMNS,
        )
        rows = manager_client.execute(sql)

        for row in rows:
            severity, match_count, first_seen, last_seen, sample_events = row
            assert match_count > 0
            assert first_seen <= last_seen
            assert len(sample_events) > 0
            assert len(sample_events) <= 10  # default max_sample_events

    def test_max_sample_events_cap(self, manager_client, dfe_db, results_table):
        """groupArray(N) caps sample_events at N."""
        sql = build_grouping_query(
            target_db=dfe_db,
            target_table=results_table,
            hunt_name="hunt_alpha",
            rule_name="priv_esc",
            customer="acme",
            group_by=["severity"],
            time_start="2026-03-03 10:00:00",
            time_end="2026-03-03 11:00:00",
            results_table_columns=RESULTS_TABLE_COLUMNS,
            max_sample_events=3,
        )
        rows = manager_client.execute(sql)

        for row in rows:
            sample_events = row[4]
            assert len(sample_events) <= 3


# ── Alert State Cooldown Tests ─────────────────────────────────


class TestAlertStateCooldown:
    def test_cooldown_lifecycle(self, manager_client, alert_state):
        """record fire → check within window (blocked) → check after window (allowed)."""
        mgr = alert_state
        hunt = f"hunt_{uuid.uuid4().hex[:6]}"

        # No prior fire — should be allowed
        assert (
            mgr.check_cooldown(manager_client, hunt, "rule_1", "acme", timedelta(hours=1)) is True
        )

        # Record fire
        mgr.record_fire(manager_client, hunt, "rule_1", "acme")

        # Check within window — should be blocked
        assert (
            mgr.check_cooldown(manager_client, hunt, "rule_1", "acme", timedelta(hours=1)) is False
        )

        # Check with zero cooldown — always allowed
        assert mgr.check_cooldown(manager_client, hunt, "rule_1", "acme", timedelta(0)) is True

    def test_per_group_cooldown(self, manager_client, alert_state):
        """Fire group A → group B still fires, group A blocked."""
        mgr = alert_state
        hunt = f"hunt_{uuid.uuid4().hex[:6]}"
        group_a = build_group_key(["source_ip"], {"source_ip": "10.0.0.1"})
        group_b = build_group_key(["source_ip"], {"source_ip": "10.0.0.2"})

        # Fire group A
        mgr.record_fire(manager_client, hunt, "rule_1", "acme", group_key=group_a)

        # Group A blocked, group B still allowed
        assert (
            mgr.check_cooldown(
                manager_client, hunt, "rule_1", "acme", timedelta(hours=1), group_key=group_a
            )
            is False
        )
        assert (
            mgr.check_cooldown(
                manager_client, hunt, "rule_1", "acme", timedelta(hours=1), group_key=group_b
            )
            is True
        )

        # Fire group B
        mgr.record_fire(manager_client, hunt, "rule_1", "acme", group_key=group_b)

        # Both now blocked
        assert (
            mgr.check_cooldown(
                manager_client, hunt, "rule_1", "acme", timedelta(hours=1), group_key=group_a
            )
            is False
        )
        assert (
            mgr.check_cooldown(
                manager_client, hunt, "rule_1", "acme", timedelta(hours=1), group_key=group_b
            )
            is False
        )

    def test_ensure_table_asserts_the_table_rather_than_creating_it(
        self, ch_client, manager_client, dfe_db
    ):
        """The schema phase creates alert_state; the manager only checks it is there."""
        absent = AlertStateManager(database=dfe_db)
        absent.ensure_table_exists(manager_client)
        assert absent._table_ensured is False
        assert ch_client.command(f"EXISTS TABLE `{dfe_db}`.alert_state") == 0

        _apply_alert_state(ch_client, dfe_db)
        present = AlertStateManager(database=dfe_db)
        present.ensure_table_exists(manager_client)
        present.ensure_table_exists(manager_client)
        assert present._table_ensured is True
