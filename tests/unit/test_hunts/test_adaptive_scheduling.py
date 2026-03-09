"""Tests for adaptive REFRESH AFTER hunt scheduling."""

from datetime import datetime
from unittest.mock import MagicMock


from dfe_engine.hunts.job import JobScheduler
from dfe_engine.hunts.cron_job import CronJob
from dfe_engine.hunts.hunt_engine import HuntEngine
from dfe_engine.settings import DFESettings, HuntsSettings


# ── JobScheduler Adaptive Mode ────────────────────────────────────


class TestJobSchedulerAdaptive:
    """Test JobScheduler adaptive rescheduling."""

    def test_enable_adaptive_mode_registers_job(self):
        scheduler = JobScheduler()
        scheduler.enable_adaptive_mode("job-1", 300)
        assert "job-1" in scheduler._adaptive_intervals
        assert scheduler._adaptive_intervals["job-1"] == 300

    def test_enable_multiple_jobs(self):
        scheduler = JobScheduler()
        scheduler.enable_adaptive_mode("job-1", 300)
        scheduler.enable_adaptive_mode("job-2", 600)
        assert len(scheduler._adaptive_intervals) == 2

    def test_rescheduled_jobs_initially_empty(self):
        scheduler = JobScheduler()
        assert len(scheduler._rescheduled_jobs) == 0

    def test_job_listener_reschedules_on_success(self):
        """After successful execution, adaptive job should be rescheduled."""
        scheduler = JobScheduler()
        scheduler.enable_adaptive_mode("test-job-0", 300)

        # Mock the APScheduler internals
        mock_job = MagicMock()
        mock_job.name = "test-job"
        mock_job.id = "test-job-0"
        mock_job.next_run_time = datetime.now()
        scheduler.scheduler.get_job = MagicMock(return_value=mock_job)
        scheduler.scheduler.reschedule_job = MagicMock()

        # Create a successful event
        event = MagicMock()
        event.job_id = "test-job-0"
        event.exception = None
        event.retval = {
            "total_execution_time": 10.5,
            "successful_queries": 3,
            "failed_queries": 0,
            "hunt_name": "test_hunt",
        }
        event.scheduled_run_time = datetime.now()

        scheduler.job_listener(event)

        # Verify rescheduling happened
        scheduler.scheduler.reschedule_job.assert_called_once()
        call_args = scheduler.scheduler.reschedule_job.call_args
        assert call_args[0][0] == "test-job-0"
        assert "test-job-0" in scheduler._rescheduled_jobs

    def test_job_listener_no_reschedule_on_error(self):
        """On error, adaptive job should NOT be rescheduled."""
        scheduler = JobScheduler()
        scheduler.enable_adaptive_mode("test-job-0", 300)

        mock_job = MagicMock()
        mock_job.name = "test-job"
        mock_job.id = "test-job-0"
        scheduler.scheduler.get_job = MagicMock(return_value=mock_job)
        scheduler.scheduler.reschedule_job = MagicMock()

        event = MagicMock()
        event.job_id = "test-job-0"
        event.exception = RuntimeError("query failed")

        scheduler.job_listener(event)

        scheduler.scheduler.reschedule_job.assert_not_called()
        assert "test-job-0" not in scheduler._rescheduled_jobs

    def test_job_listener_ignores_non_adaptive_jobs(self):
        """Non-adaptive jobs should not be rescheduled."""
        scheduler = JobScheduler()
        # No enable_adaptive_mode call

        mock_job = MagicMock()
        mock_job.name = "regular-job"
        mock_job.id = "regular-job-0"
        mock_job.next_run_time = datetime.now()
        scheduler.scheduler.get_job = MagicMock(return_value=mock_job)
        scheduler.scheduler.reschedule_job = MagicMock()

        event = MagicMock()
        event.job_id = "regular-job-0"
        event.exception = None
        event.retval = {}
        event.scheduled_run_time = datetime.now()

        scheduler.job_listener(event)
        scheduler.scheduler.reschedule_job.assert_not_called()

    def test_job_listener_handles_missing_job(self):
        """If job is gone from scheduler, listener should not crash."""
        scheduler = JobScheduler()
        scheduler.scheduler.get_job = MagicMock(return_value=None)

        event = MagicMock()
        event.job_id = "gone-job"
        event.exception = None

        # Should not raise
        scheduler.job_listener(event)

    def test_reschedule_failure_logged_not_raised(self):
        """If reschedule_job fails, error is logged, not raised."""
        scheduler = JobScheduler()
        scheduler.enable_adaptive_mode("test-job-0", 300)

        mock_job = MagicMock()
        mock_job.name = "test-job"
        mock_job.id = "test-job-0"
        mock_job.next_run_time = datetime.now()
        scheduler.scheduler.get_job = MagicMock(return_value=mock_job)
        scheduler.scheduler.reschedule_job = MagicMock(side_effect=RuntimeError("scheduler error"))

        event = MagicMock()
        event.job_id = "test-job-0"
        event.exception = None
        event.retval = {}
        event.scheduled_run_time = datetime.now()

        # Should not raise
        scheduler.job_listener(event)


# ── CronJob Adaptive Wiring ──────────────────────────────────────


class TestCronJobAdaptive:
    """Test CronJob adaptive scheduling wiring."""

    def test_accepts_scheduling_mode(self):
        cron_job = CronJob(
            hunt_log_path="/tmp/logs",
            target_config_data={},
            checkpoint_timestamp_field="timestamp_load",
            scheduling_mode="adaptive",
        )
        assert cron_job._scheduling_mode == "adaptive"

    def test_accepts_min_interval(self):
        cron_job = CronJob(
            hunt_log_path="/tmp/logs",
            target_config_data={},
            checkpoint_timestamp_field="timestamp_load",
            min_interval_seconds=600,
        )
        assert cron_job._min_interval_seconds == 600

    def test_accepts_explain_queries(self):
        cron_job = CronJob(
            hunt_log_path="/tmp/logs",
            target_config_data={},
            checkpoint_timestamp_field="timestamp_load",
            explain_queries=True,
        )
        assert cron_job._explain_queries is True

    def test_defaults_cron_mode(self):
        cron_job = CronJob(
            hunt_log_path="/tmp/logs",
            target_config_data={},
            checkpoint_timestamp_field="timestamp_load",
        )
        assert cron_job._scheduling_mode == "cron"
        assert cron_job._min_interval_seconds == 0
        assert cron_job._explain_queries is False


# ── Settings ─────────────────────────────────────────────────────


class TestHuntsSettingsAdaptive:
    """Test HuntsSettings adaptive scheduling fields."""

    def test_default_scheduling_mode_is_adaptive(self):
        settings = HuntsSettings()
        assert settings.scheduling_mode == "adaptive"

    def test_min_interval_default_zero(self):
        settings = HuntsSettings()
        assert settings.min_interval_seconds == 0

    def test_explain_queries_default_false(self):
        settings = HuntsSettings()
        assert settings.explain_queries is False

    def test_custom_values(self):
        settings = HuntsSettings(
            scheduling_mode="cron",
            min_interval_seconds=600,
            explain_queries=True,
        )
        assert settings.scheduling_mode == "cron"
        assert settings.min_interval_seconds == 600
        assert settings.explain_queries is True


# ── HuntEngine Integration ───────────────────────────────────────


class TestHuntEngineAdaptive:
    """Test HuntEngine passes adaptive settings to CronJob."""

    def test_adaptive_engine_starts(self, tmp_path):
        """Engine with adaptive mode should start without error."""
        hunt_dir = tmp_path / "hunts"
        rule_dir = tmp_path / "rules"
        hunt_dir.mkdir()
        rule_dir.mkdir()

        import yaml

        hunt_config = {
            "name": "adaptive_test",
            "cron": "*/5 * * * *",
            "log_buffer": 60,
            "global_target_table_name": "alerts",
            "global_source_table_name": "source",
            "checkpoint_timestamp_field": "timestamp_load",
            "customers": ["test_org"],
            "rules": [{"rule_name": "test_rule", "initial_checkpoint_lookback_minutes": 10}],
        }
        with open(hunt_dir / "test.yaml", "w") as f:
            yaml.dump(hunt_config, f)

        rule_template = (
            "INSERT INTO {{org_id}}.{{target_table_name}} "
            "SELECT * FROM {{org_id}}.{{source_table_name}} "
            "WHERE {timestamp_condition}"
        )
        with open(rule_dir / "test_rule.jinja2", "w") as f:
            f.write(rule_template)

        settings = DFESettings(
            hunts=HuntsSettings(
                hunt_dir=str(hunt_dir),
                rule_repo_dir=str(rule_dir),
                log_path=str(tmp_path / "logs"),
                cron_task_timeout=2,
                checkpoint_destination="file",
                checkpoint_path=str(tmp_path / "checkpoints"),
                jitter_seconds=0,
                scheduling_mode="adaptive",
                min_interval_seconds=300,
                explain_queries=False,
            ),
        )
        engine = HuntEngine(settings=settings)
        engine.start(timeout=10)
        try:
            assert engine.is_running
        finally:
            engine.stop(timeout=5)
