"""Tests for hunt execution profile capture from system.query_log."""

import pytest
from unittest.mock import MagicMock

from dfe_engine.hunts.hunt import Hunt
from dfe_engine.hunts.checkpoint import HuntCheckpointManager


# ── Hunt._capture_execution_profile ─────────────────────────────


class TestCaptureExecutionProfile:
    """Test execution profile capture from system.query_log."""

    def test_captures_profile_on_success(self):
        ch_client = MagicMock()
        ch_client.execute.return_value = [(1000, 2048, 4096, 5)]

        profile = Hunt._capture_execution_profile(ch_client, "qid-123", "rule_a")

        assert profile == {
            "read_rows": 1000,
            "read_bytes": 2048,
            "memory_usage": 4096,
            "result_rows": 5,
        }

    def test_returns_empty_on_no_rows(self):
        ch_client = MagicMock()
        ch_client.execute.return_value = []

        profile = Hunt._capture_execution_profile(ch_client, "qid-456", "rule_b")
        assert profile == {}

    def test_returns_empty_on_exception(self):
        ch_client = MagicMock()
        ch_client.execute.side_effect = Exception("system.query_log unavailable")

        profile = Hunt._capture_execution_profile(ch_client, "qid-789", "rule_c")
        assert profile == {}

    def test_queries_with_correct_query_id(self):
        ch_client = MagicMock()
        ch_client.execute.return_value = [(100, 200, 300, 1)]

        Hunt._capture_execution_profile(ch_client, "my-query-id", "rule_d")

        args = ch_client.execute.call_args
        assert "system.query_log" in args[0][0]
        assert args[1]["parameters"]["qid"] == "my-query-id"

    def test_returns_empty_on_none_result(self):
        ch_client = MagicMock()
        ch_client.execute.return_value = None

        profile = Hunt._capture_execution_profile(ch_client, "qid-nil", "rule_e")
        assert profile == {}


# ── Checkpoint migration ────────────────────────────────────────


class TestCheckpointProfileColumns:
    """Test checkpoint table includes execution profile columns."""

    def test_create_table_has_profile_columns(self):
        mgr = HuntCheckpointManager()
        ch_client = MagicMock()

        # Simulate database_exists=False, table_exists=False
        def mock_execute(sql, *args, **kwargs):
            if "system.databases" in sql:
                return []
            if "system.tables" in sql:
                return []
            return None

        ch_client.execute.side_effect = mock_execute

        mgr.ensure_table_exists(ch_client)

        # Find the CREATE TABLE call
        create_table_calls = [
            call for call in ch_client.execute.call_args_list
            if "CREATE TABLE" in str(call)
        ]
        assert len(create_table_calls) >= 1
        create_sql = str(create_table_calls[0])
        assert "read_rows" in create_sql
        assert "read_bytes" in create_sql
        assert "memory_usage" in create_sql
        assert "result_rows" in create_sql

    def test_migrate_adds_profile_columns(self):
        mgr = HuntCheckpointManager()
        ch_client = MagicMock()

        mgr.migrate_table_if_needed(ch_client)

        alter_calls = [
            str(call) for call in ch_client.execute.call_args_list
        ]
        profile_cols = ["read_rows", "read_bytes", "memory_usage", "result_rows"]
        for col in profile_cols:
            assert any(col in call for call in alter_calls), (
                f"Migration should add {col}"
            )

    def test_batch_checkpoint_includes_profile(self):
        mgr = HuntCheckpointManager()
        ch_client = MagicMock()

        # Ensure table appears to exist
        ch_client.execute.return_value = [(1,)]

        checkpoints = [{
            "customer_name": "acme",
            "rule_name": "rule_1",
            "thread_id": "t1",
            "log_buffer": 10,
            "query_schedule_time": "2026-03-01 00:00:00",
            "execution_time": "2026-03-01 00:00:01",
            "end_time": "2026-03-01 00:00:02",
            "previous_successful_checkpoint": "2026-02-28 23:55:00",
            "query_checkpoint_time": "2026-03-01 00:00:00",
            "execution_time_ms": 1500,
            "hunt_name": "test_hunt",
            "query_id": "q-123",
            "explain_plan": "",
            "explain_duration_ms": 0,
            "scheduling_mode": "adaptive",
            "read_rows": 50000,
            "read_bytes": 1024000,
            "memory_usage": 2048000,
            "result_rows": 3,
        }]

        mgr.create_batch_checkpoint_clickhouse(ch_client, checkpoints)

        # Verify the INSERT includes profile columns
        insert_call = ch_client.execute.call_args
        insert_sql = insert_call[0][0]
        assert "read_rows" in insert_sql
        assert "read_bytes" in insert_sql
        assert "memory_usage" in insert_sql
        assert "result_rows" in insert_sql

        # Verify data tuple has 19 elements (15 original + 4 profile)
        data = insert_call[0][1]
        assert len(data) == 1
        assert len(data[0]) == 19
        assert data[0][15] == 50000   # read_rows
        assert data[0][16] == 1024000  # read_bytes
        assert data[0][17] == 2048000  # memory_usage
        assert data[0][18] == 3       # result_rows
