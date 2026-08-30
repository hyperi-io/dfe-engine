"""Tests for hunt alert grouping + cooldown."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from dfe_engine.hunts.alert_grouping import (
    AlertGroupingConfig,
    AlertStateManager,
    build_group_key,
    build_grouping_query,
    parse_duration,
)

# ── parse_duration ───────────────────────────────────────────────


class TestParseDuration:
    def test_hours(self):
        assert parse_duration("1h") == timedelta(hours=1)

    def test_minutes(self):
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_seconds(self):
        assert parse_duration("300s") == timedelta(seconds=300)

    def test_days(self):
        assert parse_duration("7d") == timedelta(days=7)

    def test_case_insensitive(self):
        assert parse_duration("1H") == timedelta(hours=1)
        assert parse_duration("30M") == timedelta(minutes=30)

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("abc")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("")

    def test_missing_unit_raises(self):
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("100")

    def test_zero(self):
        assert parse_duration("0h") == timedelta(0)


# ── AlertGroupingConfig ─────────────────────────────────────────


class TestAlertGroupingConfig:
    def test_defaults(self):
        cfg = AlertGroupingConfig()
        assert cfg.group_by == []
        assert cfg.cooldown == "1h"
        assert cfg.max_alerts_per_run == 0
        assert cfg.max_sample_events == 10
        assert cfg.has_group_by is False

    def test_custom_group_by(self):
        cfg = AlertGroupingConfig(group_by=["source_ip", "process_name"])
        assert cfg.has_group_by is True
        assert len(cfg.group_by) == 2

    def test_cooldown_td(self):
        cfg = AlertGroupingConfig(cooldown="2h")
        assert cfg.cooldown_td == timedelta(hours=2)

    def test_invalid_cooldown_raises(self):
        with pytest.raises(ValueError):
            AlertGroupingConfig(cooldown="invalid")

    def test_from_dict(self):
        """Simulate parsing from hunt YAML."""
        data = {
            "group_by": ["severity", "source_ip"],
            "cooldown": "30m",
            "max_alerts_per_run": 50,
            "max_sample_events": 5,
        }
        cfg = AlertGroupingConfig(**data)
        assert cfg.group_by == ["severity", "source_ip"]
        assert cfg.cooldown_td == timedelta(minutes=30)
        assert cfg.max_alerts_per_run == 50
        assert cfg.max_sample_events == 5


# ── build_group_key ────────────────────────────────────────────


class TestBuildGroupKey:
    def test_single_field(self):
        key = build_group_key(["source_ip"], {"source_ip": "1.2.3.4"})
        assert key == "source_ip=1.2.3.4"

    def test_multiple_fields(self):
        key = build_group_key(
            ["severity", "source_ip"],
            {"severity": "high", "source_ip": "1.2.3.4"},
        )
        assert key == "severity=high|source_ip=1.2.3.4"

    def test_empty_group_by(self):
        key = build_group_key([], {"source_ip": "1.2.3.4"})
        assert key == ""

    def test_missing_field_uses_empty(self):
        key = build_group_key(["source_ip"], {})
        assert key == "source_ip="

    def test_pipe_in_value_escaped(self):
        key = build_group_key(["cmd"], {"cmd": "a|b"})
        assert key == "cmd=a\\|b"

    def test_equals_in_value_escaped(self):
        key = build_group_key(["cmd"], {"cmd": "x=y"})
        assert key == "cmd=x\\=y"

    def test_backslash_in_value_escaped(self):
        key = build_group_key(["path"], {"path": r"C:\Windows"})
        assert key == r"path=C:\\Windows"

    def test_ordering_follows_group_by_list(self):
        """Key order is determined by group_by list, not dict key order."""
        key = build_group_key(
            ["b_field", "a_field"],
            {"a_field": "aaa", "b_field": "bbb"},
        )
        assert key == "b_field=bbb|a_field=aaa"


# ── build_grouping_query ────────────────────────────────────────


class TestBuildGroupingQuery:
    """Tests for the post-INSERT aggregation query builder."""

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

    def test_empty_group_by_returns_none(self):
        result = build_grouping_query(
            target_db="acme",
            target_table="logs_alerts",
            hunt_name="test_hunt",
            rule_name="test_rule",
            customer="acme",
            group_by=[],
            time_start="2026-01-01 00:00:00",
            time_end="2026-01-01 01:00:00",
            results_table_columns=self.RESULTS_COLS,
        )
        assert result is None

    def test_single_direct_column(self):
        sql = build_grouping_query(
            target_db="acme",
            target_table="logs_alerts",
            hunt_name="test_hunt",
            rule_name="test_rule",
            customer="acme",
            group_by=["severity"],
            time_start="2026-01-01 00:00:00",
            time_end="2026-01-01 01:00:00",
            results_table_columns=self.RESULTS_COLS,
        )
        assert "severity" in sql
        assert "GROUP BY severity" in sql
        assert "JSONExtract" not in sql  # direct column, no JSON extraction
        assert "count(*) AS match_count" in sql
        assert "min(_timestamp) AS first_seen" in sql
        assert "max(_timestamp) AS last_seen" in sql

    def test_single_json_field(self):
        sql = build_grouping_query(
            target_db="acme",
            target_table="logs_alerts",
            hunt_name="test_hunt",
            rule_name="test_rule",
            customer="acme",
            group_by=["source_ip"],
            time_start="2026-01-01 00:00:00",
            time_end="2026-01-01 01:00:00",
            results_table_columns=self.RESULTS_COLS,
        )
        assert "JSONExtractString(_json, 'source_ip') AS source_ip" in sql
        assert "GROUP BY source_ip" in sql

    def test_multiple_group_by_mixed(self):
        """Mix of direct column and JSON field."""
        sql = build_grouping_query(
            target_db="acme",
            target_table="logs_alerts",
            hunt_name="test_hunt",
            rule_name="test_rule",
            customer="acme",
            group_by=["severity", "source_ip"],
            time_start="2026-01-01 00:00:00",
            time_end="2026-01-01 01:00:00",
            results_table_columns=self.RESULTS_COLS,
        )
        # severity is direct, source_ip is JSON
        assert "severity" in sql
        assert "JSONExtractString(_json, 'source_ip') AS source_ip" in sql
        assert "GROUP BY severity, source_ip" in sql

    def test_where_clause(self):
        sql = build_grouping_query(
            target_db="acme",
            target_table="logs_alerts",
            hunt_name="windows_hunt",
            rule_name="win_priv_esc",
            customer="acme_corp",
            group_by=["severity"],
            time_start="2026-01-01 00:00:00",
            time_end="2026-01-01 01:00:00",
            results_table_columns=self.RESULTS_COLS,
        )
        assert "hunt_name = 'windows_hunt'" in sql
        assert "rule_name = 'win_priv_esc'" in sql
        assert "_org_id = 'acme_corp'" in sql
        assert "_timestamp >= '2026-01-01 00:00:00'" in sql
        assert "_timestamp < '2026-01-01 01:00:00'" in sql

    def test_order_by_match_count(self):
        sql = build_grouping_query(
            target_db="acme",
            target_table="logs_alerts",
            hunt_name="test",
            rule_name="rule",
            customer="acme",
            group_by=["severity"],
            time_start="2026-01-01 00:00:00",
            time_end="2026-01-01 01:00:00",
            results_table_columns=self.RESULTS_COLS,
        )
        assert "ORDER BY match_count DESC" in sql

    def test_max_sample_events(self):
        sql = build_grouping_query(
            target_db="acme",
            target_table="logs_alerts",
            hunt_name="test",
            rule_name="rule",
            customer="acme",
            group_by=["severity"],
            time_start="2026-01-01 00:00:00",
            time_end="2026-01-01 01:00:00",
            results_table_columns=self.RESULTS_COLS,
            max_sample_events=25,
        )
        assert "groupArray(25)(_json)" in sql

    def test_from_table_reference(self):
        sql = build_grouping_query(
            target_db="acme",
            target_table="logs_alerts",
            hunt_name="test",
            rule_name="rule",
            customer="acme",
            group_by=["severity"],
            time_start="2026-01-01 00:00:00",
            time_end="2026-01-01 01:00:00",
            results_table_columns=self.RESULTS_COLS,
        )
        assert "FROM `acme`.`logs_alerts`" in sql

    def test_from_reference_quotes_a_hyphenated_table(self):
        sql = build_grouping_query(
            target_db="acme",
            target_table="filebeat-alerts",
            hunt_name="test",
            rule_name="rule",
            customer="acme",
            group_by=["severity"],
            time_start="2026-01-01 00:00:00",
            time_end="2026-01-01 01:00:00",
            results_table_columns=self.RESULTS_COLS,
        )
        assert "FROM `acme`.`filebeat-alerts`" in sql


# ── AlertStateManager ────────────────────────────────────────────


class TestAlertStateManager:
    def test_ddl_contains_expected_structure(self):
        mgr = AlertStateManager()
        ddl = mgr.get_ddl()
        assert "CREATE TABLE IF NOT EXISTS `dfe`.`alert_state`" in ddl
        assert "hunt_name" in ddl
        assert "rule_name" in ddl
        assert "_org_id" in ddl
        assert "last_fired_at" in ddl
        assert "fire_count" in ddl
        assert "ReplacingMergeTree" in ddl
        assert "TTL" in ddl

    def test_ddl_custom_database(self):
        mgr = AlertStateManager(database="custom_db")
        ddl = mgr.get_ddl()
        assert "`custom_db`.`alert_state`" in ddl

    def test_ensure_table_exists_idempotent(self):
        mgr = AlertStateManager()
        ch_client = MagicMock()
        # Nothing exists yet, so the applier creates both.
        ch_client.query.return_value.result_rows = []
        mgr.ensure_table_exists(ch_client)
        first = ch_client.command.call_count
        assert first == 2  # CREATE DATABASE + CREATE TABLE
        mgr.ensure_table_exists(ch_client)
        assert ch_client.command.call_count == first  # cached, no re-run

    def test_check_cooldown_no_prior_fire(self):
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch_client = MagicMock()
        ch_client.execute.return_value = []  # No rows = never fired

        can_fire = mgr.check_cooldown(ch_client, "hunt1", "rule1", "acme", timedelta(hours=1))
        assert can_fire is True

    def test_check_cooldown_within_window(self):
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch_client = MagicMock()
        # Last fired 10 minutes ago
        recent = datetime.now(UTC) - timedelta(minutes=10)
        ch_client.execute.return_value = [(recent,)]

        can_fire = mgr.check_cooldown(ch_client, "hunt1", "rule1", "acme", timedelta(hours=1))
        assert can_fire is False

    def test_check_cooldown_after_window(self):
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch_client = MagicMock()
        # Last fired 2 hours ago
        old = datetime.now(UTC) - timedelta(hours=2)
        ch_client.execute.return_value = [(old,)]

        can_fire = mgr.check_cooldown(ch_client, "hunt1", "rule1", "acme", timedelta(hours=1))
        assert can_fire is True

    def test_check_cooldown_zero_always_fires(self):
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch_client = MagicMock()

        can_fire = mgr.check_cooldown(ch_client, "hunt1", "rule1", "acme", timedelta(0))
        assert can_fire is True
        # Should not even query ClickHouse
        ch_client.execute.assert_not_called()

    def test_check_cooldown_error_fails_open(self):
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch_client = MagicMock()
        ch_client.execute.side_effect = Exception("connection lost")

        can_fire = mgr.check_cooldown(ch_client, "hunt1", "rule1", "acme", timedelta(hours=1))
        assert can_fire is True  # Fail-open

    def test_record_fire(self):
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch_client = MagicMock()
        fired_at = datetime(2026, 3, 3, 12, 0, 0, tzinfo=UTC)

        mgr.record_fire(ch_client, "hunt1", "rule1", "acme", fired_at=fired_at)
        ch_client.execute.assert_called_once()
        call_args = ch_client.execute.call_args
        assert "INSERT INTO dfe.alert_state" in call_args[0][0]

    def test_ddl_contains_group_key(self):
        mgr = AlertStateManager()
        ddl = mgr.get_ddl()
        assert "group_key" in ddl
        assert "`_org_id`, `group_key`)" in ddl  # ORDER BY includes group_key

    def test_check_cooldown_with_group_key(self):
        """Different groups have independent cooldown state."""
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch_client = MagicMock()

        # Group A fired recently — should block
        recent = datetime.now(UTC) - timedelta(minutes=10)
        ch_client.execute.return_value = [(recent,)]

        can_fire_a = mgr.check_cooldown(
            ch_client,
            "hunt1",
            "rule1",
            "acme",
            timedelta(hours=1),
            group_key="source_ip=1.2.3.4",
        )
        assert can_fire_a is False
        # Verify group_key was passed in the query parameters
        call_params = ch_client.execute.call_args[1].get(
            "parameters",
            ch_client.execute.call_args[0][1] if len(ch_client.execute.call_args[0]) > 1 else {},
        )
        assert call_params["group_key"] == "source_ip=1.2.3.4"

    def test_check_cooldown_default_group_key(self):
        """Default group_key='' preserves backward compat for ungrouped alerts."""
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch_client = MagicMock()
        ch_client.execute.return_value = []

        can_fire = mgr.check_cooldown(ch_client, "hunt1", "rule1", "acme", timedelta(hours=1))
        assert can_fire is True
        call_kwargs = ch_client.execute.call_args
        params = call_kwargs[1].get(
            "parameters", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {}
        )
        assert params["group_key"] == ""

    def test_record_fire_with_group_key(self):
        """record_fire includes group_key in INSERT values."""
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch_client = MagicMock()
        fired_at = datetime(2026, 3, 3, 12, 0, 0, tzinfo=UTC)

        mgr.record_fire(
            ch_client,
            "hunt1",
            "rule1",
            "acme",
            group_key="severity=high|source_ip=10.0.0.1",
            fired_at=fired_at,
        )
        ch_client.execute.assert_called_once()
        call_args = ch_client.execute.call_args
        # The group_key should be in the INSERT parameters
        insert_params = call_args[1].get(
            "parameters", call_args[0][1] if len(call_args[0]) > 1 else []
        )
        assert "severity=high|source_ip=10.0.0.1" in insert_params[0]


# ── Dispatch Integration ────────────────────────────────────────


class TestDispatchIntegration:
    """Test the Hunt._dispatch_alerts interaction with grouping."""

    def test_ungrouped_dispatch_uses_result_rows(self):
        """Without alert_grouping, fires per-rule with actual result_rows."""
        from dfe_engine.hunts.alert import AlertConfig, AlertDispatcher

        config = AlertConfig(
            channels=["slack://test"],
            triggers=[{"when": "result_count > 0"}],
        )
        dispatcher = AlertDispatcher(config)

        with patch("apprise.Apprise.notify", return_value=True):
            sent = dispatcher.evaluate_and_send(
                hunt_name="test_hunt",
                customer="acme",
                rule_name="test_rule",
                result_count=42,
            )
        assert sent is True

    def test_grouped_dispatch_with_group_context(self):
        """group_context enriches the alert body."""
        from dfe_engine.hunts.alert import AlertConfig, AlertDispatcher

        config = AlertConfig(
            channels=["slack://test"],
            triggers=[{"when": "result_count > 0"}],
            body_template=(
                "Hunt **{hunt_name}**: **{match_count}** matches "
                "({group_fields}) from {first_seen} to {last_seen}"
            ),
        )
        dispatcher = AlertDispatcher(config)

        group_ctx = {
            "match_count": 10000,
            "first_seen": "2026-01-01 08:00:00",
            "last_seen": "2026-01-01 09:15:00",
            "group_fields": {"source_ip": "3.14.159.26"},
        }

        with patch("apprise.Apprise.notify", return_value=True) as mock_notify:
            sent = dispatcher.evaluate_and_send(
                hunt_name="test_hunt",
                customer="acme",
                rule_name="test_rule",
                result_count=10000,
                group_context=group_ctx,
            )
        assert sent is True
        body = mock_notify.call_args.kwargs.get("body", mock_notify.call_args[1].get("body", ""))
        assert "10000" in body
        assert "source_ip=3.14.159.26" in body

    def test_safe_format_missing_keys(self):
        """Templates with group placeholders don't crash for ungrouped alerts."""
        from dfe_engine.hunts.alert import AlertConfig, AlertDispatcher

        config = AlertConfig(
            channels=["slack://test"],
            triggers=[{"when": "result_count > 0"}],
            body_template="Hunt {hunt_name}: {match_count} matches ({group_fields})",
        )
        dispatcher = AlertDispatcher(config)

        with patch("apprise.Apprise.notify", return_value=True) as mock_notify:
            sent = dispatcher.evaluate_and_send(
                hunt_name="test_hunt",
                customer="acme",
                rule_name="test_rule",
                result_count=5,
                # No group_context — template should handle missing keys
            )
        assert sent is True
        body = mock_notify.call_args.kwargs.get("body", mock_notify.call_args[1].get("body", ""))
        # Missing keys should appear as literal {key} placeholders, not crash
        assert "{match_count}" in body or "match_count" in body

    def test_alert_grouping_config_from_yaml(self):
        """AlertGroupingConfig is correctly parsed from hunt YAML data."""
        hunt_data = {
            "alert_grouping": {
                "group_by": ["source_ip", "event_type"],
                "cooldown": "2h",
                "max_alerts_per_run": 100,
                "max_sample_events": 20,
            }
        }
        grouping_data = hunt_data.get("alert_grouping")
        cfg = AlertGroupingConfig(**grouping_data)
        assert cfg.group_by == ["source_ip", "event_type"]
        assert cfg.cooldown_td == timedelta(hours=2)
        assert cfg.max_alerts_per_run == 100
        assert cfg.max_sample_events == 20

    def test_no_alert_grouping_returns_none(self):
        """When alert_grouping is absent from YAML, result is None."""
        hunt_data = {"name": "test_hunt", "cron": "*/5 * * * *"}
        grouping_data = hunt_data.get("alert_grouping")
        assert grouping_data is None
