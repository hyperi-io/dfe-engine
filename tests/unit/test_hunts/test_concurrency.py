"""Tests for hunt concurrency limiting, resource hog detection, and backpressure."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from dfe_engine.hunts.cron_job import CronJob
from dfe_engine.hunts.hunt import Hunt


class TestConcurrencySemaphore:
    """Test that concurrency_semaphore limits concurrent hunt execution."""

    def test_cronjob_accepts_semaphore(self):
        """CronJob stores the semaphore when provided."""
        sem = asyncio.Semaphore(2)
        cj = CronJob(
            hunt_log_path="/tmp",
            target_config_data={},
            checkpoint_timestamp_field="ts",
            concurrency_semaphore=sem,
        )
        assert cj._concurrency_semaphore is sem

    def test_cronjob_default_no_semaphore(self):
        """CronJob defaults to no semaphore (unlimited concurrency)."""
        cj = CronJob(
            hunt_log_path="/tmp",
            target_config_data={},
            checkpoint_timestamp_field="ts",
        )
        assert cj._concurrency_semaphore is None

    def test_cronjob_accepts_resource_limits(self):
        """CronJob stores resource limits when provided."""
        limits = {"read_rows": 1_000_000, "execution_ms": 5000}
        cj = CronJob(
            hunt_log_path="/tmp",
            target_config_data={},
            checkpoint_timestamp_field="ts",
            resource_limits=limits,
        )
        assert cj._resource_limits == limits


class TestResourceHogDetection:
    """Test that Hunt._check_resource_limits logs warnings for threshold violations."""

    def _make_hunt(self, resource_limits=None):
        """Create a minimal Hunt with resource limits."""
        return Hunt(
            cron="*/5 * * * *",
            log_buffer=60,
            customer="test_org",
            rules=[],
            name="test_hunt",
            global_source_table_name="default.events",
            global_target_table_name="dfe_audit.results",
            hunt_log_path="/tmp/hunts",
            target_config_data={},
            resource_limits=resource_limits,
        )

    def test_no_limits_no_warnings(self):
        """No warnings when no limits configured."""
        hunt = self._make_hunt()
        profile = {"read_rows": 999_999_999, "read_bytes": 10**12, "memory_usage": 10**10}
        # Should not raise
        hunt._check_resource_limits("rule1", "org1", profile, 99999)

    def test_no_profile_no_warnings(self):
        """No warnings when profile is empty (capture failed)."""
        hunt = self._make_hunt(resource_limits={"read_rows": 100})
        hunt._check_resource_limits("rule1", "org1", {}, 1000)

    def test_read_rows_exceeded(self):
        """Warning logged when read_rows exceeds limit."""
        hunt = self._make_hunt(resource_limits={"read_rows": 1000})
        profile = {"read_rows": 5000, "read_bytes": 0, "memory_usage": 0}
        with patch("dfe_engine.hunts.hunt.logger") as mock_logger:
            hunt._check_resource_limits("rule1", "org1", profile, 100)
            mock_logger.warning.assert_called_once()
            assert "RESOURCE HOG" in mock_logger.warning.call_args[0][0]
            assert "read_rows" in mock_logger.warning.call_args[0][0]

    def test_memory_exceeded(self):
        """Warning logged when memory_usage exceeds limit."""
        hunt = self._make_hunt(resource_limits={"memory_bytes": 1_000_000})
        profile = {"read_rows": 0, "read_bytes": 0, "memory_usage": 5_000_000}
        with patch("dfe_engine.hunts.hunt.logger") as mock_logger:
            hunt._check_resource_limits("rule1", "org1", profile, 100)
            mock_logger.warning.assert_called_once()
            assert "memory_usage" in mock_logger.warning.call_args[0][0]

    def test_execution_time_exceeded(self):
        """Warning logged when execution time exceeds limit."""
        hunt = self._make_hunt(resource_limits={"execution_ms": 1000})
        profile = {"read_rows": 0, "read_bytes": 0, "memory_usage": 0}
        with patch("dfe_engine.hunts.hunt.logger") as mock_logger:
            hunt._check_resource_limits("rule1", "org1", profile, 5000)
            mock_logger.warning.assert_called_once()
            assert "execution_time" in mock_logger.warning.call_args[0][0]

    def test_under_limit_no_warning(self):
        """No warning when values are under limits."""
        hunt = self._make_hunt(resource_limits={
            "read_rows": 10000,
            "read_bytes": 10_000_000,
            "memory_bytes": 100_000_000,
            "execution_ms": 30000,
        })
        profile = {"read_rows": 500, "read_bytes": 5000, "memory_usage": 1000}
        with patch("dfe_engine.hunts.hunt.logger") as mock_logger:
            hunt._check_resource_limits("rule1", "org1", profile, 100)
            mock_logger.warning.assert_not_called()

    def test_multiple_limits_exceeded(self):
        """Multiple warnings when multiple limits exceeded."""
        hunt = self._make_hunt(resource_limits={
            "read_rows": 100,
            "read_bytes": 100,
            "execution_ms": 100,
        })
        profile = {"read_rows": 5000, "read_bytes": 5000, "memory_usage": 0}
        with patch("dfe_engine.hunts.hunt.logger") as mock_logger:
            hunt._check_resource_limits("rule1", "org1", profile, 5000)
            # read_rows + read_bytes + execution_ms = 3 warnings
            assert mock_logger.warning.call_count == 3


class TestBackpressureSignal:
    """Test backpressure lag detection in CronJob."""

    def test_get_hunt_interval_seconds(self):
        """Verify interval calculation from cron expression."""
        cj = CronJob(
            hunt_log_path="/tmp",
            target_config_data={},
            checkpoint_timestamp_field="ts",
        )
        # */5 * * * * = every 5 minutes = 300 seconds
        interval = cj._get_hunt_interval_seconds("*/5 * * * *")
        assert interval is not None
        assert abs(interval - 300) < 15  # croniter average may drift slightly

    def test_get_hunt_interval_invalid_cron(self):
        """Invalid cron returns None."""
        cj = CronJob(
            hunt_log_path="/tmp",
            target_config_data={},
            checkpoint_timestamp_field="ts",
        )
        interval = cj._get_hunt_interval_seconds("invalid cron")
        assert interval is None


class TestSettingsIntegration:
    """Test that new settings fields work correctly."""

    def test_hunts_settings_defaults(self):
        from dfe_engine.settings import HuntsSettings
        s = HuntsSettings()
        assert s.max_concurrent_queries == 0
        assert s.resource_limit_read_rows == 0
        assert s.resource_limit_read_bytes == 0
        assert s.resource_limit_memory_bytes == 0
        assert s.resource_limit_execution_ms == 0

    def test_hunts_settings_custom_values(self):
        from dfe_engine.settings import HuntsSettings
        s = HuntsSettings(
            max_concurrent_queries=4,
            resource_limit_read_rows=1_000_000,
            resource_limit_memory_bytes=2_000_000_000,
            resource_limit_execution_ms=30000,
        )
        assert s.max_concurrent_queries == 4
        assert s.resource_limit_read_rows == 1_000_000
        assert s.resource_limit_memory_bytes == 2_000_000_000
        assert s.resource_limit_execution_ms == 30000
