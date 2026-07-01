"""Tests for EXPLAIN plan capture in hunt query execution."""

from dfe_engine.hunts.checkpoint import HuntCheckpointManager
from dfe_engine.hunts.hunt import Hunt

# ── Hunt EXPLAIN Configuration ────────────────────────────────────


class TestHuntExplainConfig:
    """Test Hunt accepts explain_queries parameter."""

    def test_explain_disabled_by_default(self, tmp_path):
        hunt = Hunt(
            cron="*/5 * * * *",
            log_buffer=60,
            customer="test_org",
            rules=[{"rule_name": "r1"}],
            name="test_hunt",
            global_source_table_name="source",
            global_target_table_name="target",
            hunt_log_path=str(tmp_path / "logs"),
            target_config_data={},
        )
        assert hunt.explain_queries is False

    def test_explain_enabled(self, tmp_path):
        hunt = Hunt(
            cron="*/5 * * * *",
            log_buffer=60,
            customer="test_org",
            rules=[{"rule_name": "r1"}],
            name="test_hunt",
            global_source_table_name="source",
            global_target_table_name="target",
            hunt_log_path=str(tmp_path / "logs"),
            target_config_data={},
            explain_queries=True,
        )
        assert hunt.explain_queries is True


# ── Checkpoint Schema Extension ──────────────────────────────────


class TestCheckpointSchemaExtension:
    """Test checkpoint manager handles new columns."""

    def test_checkpoint_manager_has_migrate_method(self):
        mgr = HuntCheckpointManager()
        assert hasattr(mgr, "migrate_table_if_needed")

    def test_batch_checkpoint_accepts_explain_fields(self):
        """Verify the batch checkpoint data builder handles explain fields."""
        from datetime import datetime

        HuntCheckpointManager()

        checkpoint = {
            "_org_id": "test_org",
            "rule_name": "test_rule",
            "thread_id": "t1",
            "log_buffer": 60,
            "query_schedule_time": "2026-03-01 10:00:00",
            "execution_time": "2026-03-01 10:00:01",
            "end_time": "2026-03-01 10:00:05",
            "previous_successful_checkpoint": "2026-03-01 09:55:00",
            "query_checkpoint_time": "2026-03-01 09:59:00",
            "execution_time_ms": 4000,
            "hunt_name": "test_hunt",
            "query_id": "q1",
            "explain_plan": "ReadFromMergeTree\n  Filter",
            "explain_duration_ms": 50,
            "scheduling_mode": "adaptive",
        }

        # Build the data tuple as the batch method would
        data = (
            str(checkpoint.get("_org_id", "na")),
            str(checkpoint.get("rule_name", "na")),
            str(checkpoint.get("thread_id", "na")),
            int(checkpoint.get("log_buffer", 0)),
            datetime.strptime(checkpoint.get("query_schedule_time"), "%Y-%m-%d %H:%M:%S"),
            datetime.strptime(checkpoint.get("execution_time"), "%Y-%m-%d %H:%M:%S"),
            datetime.strptime(checkpoint.get("end_time"), "%Y-%m-%d %H:%M:%S"),
            datetime.strptime(
                checkpoint.get("previous_successful_checkpoint"), "%Y-%m-%d %H:%M:%S"
            ),
            datetime.strptime(checkpoint.get("query_checkpoint_time"), "%Y-%m-%d %H:%M:%S"),
            int(checkpoint.get("execution_time_ms", 0)),
            str(checkpoint.get("hunt_name", "na")),
            str(checkpoint.get("query_id", "na")),
            str(checkpoint.get("explain_plan") or ""),
            int(checkpoint.get("explain_duration_ms") or 0),
            str(checkpoint.get("scheduling_mode", "adaptive")),
        )

        assert len(data) == 15
        assert data[12] == "ReadFromMergeTree\n  Filter"
        assert data[13] == 50
        assert data[14] == "adaptive"

    def test_batch_checkpoint_handles_missing_explain(self):
        """Checkpoint without explain fields should use safe defaults."""

        checkpoint = {
            "_org_id": "org",
            "rule_name": "rule",
            "thread_id": "t",
            "log_buffer": 60,
            "query_schedule_time": "2026-03-01 10:00:00",
            "execution_time": "2026-03-01 10:00:00",
            "end_time": "2026-03-01 10:00:00",
            "previous_successful_checkpoint": "2026-03-01 09:55:00",
            "query_checkpoint_time": "2026-03-01 09:59:00",
            "execution_time_ms": 100,
            "hunt_name": "h",
            "query_id": "q",
            # No explain fields!
        }

        explain_plan = str(checkpoint.get("explain_plan") or "")
        explain_duration_ms = int(checkpoint.get("explain_duration_ms") or 0)
        scheduling_mode = str(checkpoint.get("scheduling_mode", "adaptive"))

        assert explain_plan == ""
        assert explain_duration_ms == 0
        assert scheduling_mode == "adaptive"
