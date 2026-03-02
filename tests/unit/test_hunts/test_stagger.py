"""Tests for compute_stagger_offsets() — even load spreading algorithm."""

import pytest

from dfe_engine.hunts.cron_job import compute_stagger_offsets


class TestComputeStaggerOffsets:
    """Test even distribution of staggered cron expressions."""

    def test_single_customer_returns_original(self):
        """Single customer should get the original cron expression unchanged."""
        result = compute_stagger_offsets("*/5 * * * *", total_jobs=1, jitter_seconds=10)
        assert len(result) == 1
        assert result[0] == ("*/5 * * * *", 10)

    def test_zero_jobs_returns_empty(self):
        """Zero jobs should return empty list."""
        result = compute_stagger_offsets("*/5 * * * *", total_jobs=0)
        assert result == []

    def test_per_minute_cron_no_stagger(self):
        """Per-minute cron (* * * * *) should return identical crons with jitter only."""
        result = compute_stagger_offsets("* * * * *", total_jobs=5, jitter_seconds=15)
        assert len(result) == 5
        for cron, jitter in result:
            assert cron == "* * * * *"
            assert jitter == 15

    def test_step_minute_two_customers(self):
        """Two customers on */10 should get offsets 0 and 5."""
        result = compute_stagger_offsets("*/10 * * * *", total_jobs=2, jitter_seconds=10)
        assert len(result) == 2
        # First customer: offset 0
        assert result[0][0] == "0/10 * * * *"
        # Second customer: offset 5
        assert result[1][0] == "5/10 * * * *"

    def test_step_minute_five_customers(self):
        """Five customers on */10 should get offsets 0, 2, 4, 6, 8."""
        result = compute_stagger_offsets("*/10 * * * *", total_jobs=5, jitter_seconds=10)
        assert len(result) == 5
        offsets = [r[0].split("/")[0] for r in result]
        assert offsets == ["0", "2", "4", "6", "8"]

    def test_step_minute_ten_customers(self):
        """Ten customers on */10 should get offsets 0-9."""
        result = compute_stagger_offsets("*/10 * * * *", total_jobs=10, jitter_seconds=5)
        assert len(result) == 10
        offsets = [r[0].split("/")[0] for r in result]
        assert offsets == [str(i) for i in range(10)]

    def test_fixed_minute_two_customers(self):
        """Two customers on '0 * * * *' (hourly) should get 0 and 30."""
        result = compute_stagger_offsets("0 * * * *", total_jobs=2, jitter_seconds=10)
        assert len(result) == 2
        assert result[0][0] == "0 * * * *"
        assert result[1][0] == "30 * * * *"

    def test_fixed_minute_three_customers(self):
        """Three customers on '0 * * * *' (hourly) should spread across 60 minutes."""
        result = compute_stagger_offsets("0 * * * *", total_jobs=3, jitter_seconds=10)
        assert len(result) == 3
        minutes = [r[0].split(" ")[0] for r in result]
        assert minutes == ["0", "20", "40"]

    def test_long_interval_two_customers(self):
        """Two customers on '0 */6 * * *' should distribute across 6 hours."""
        result = compute_stagger_offsets("0 */6 * * *", total_jobs=2, jitter_seconds=10)
        assert len(result) == 2
        # First at offset 0: "0 0/6 * * *"
        assert result[0][0] == "0 0/6 * * *"
        # Second at offset 180 min = 3 hours: "0 3/6 * * *"
        assert result[1][0] == "0 3/6 * * *"

    def test_jitter_value_passed_through(self):
        """Jitter value should be passed through in all results."""
        result = compute_stagger_offsets("*/5 * * * *", total_jobs=3, jitter_seconds=20)
        for _, jitter in result:
            assert jitter == 20

    def test_default_jitter_is_15(self):
        """Default jitter should be 15 seconds."""
        result = compute_stagger_offsets("*/5 * * * *", total_jobs=1)
        assert result[0][1] == 15

    def test_invalid_cron_returns_copies(self):
        """Invalid cron expression (wrong field count) should return copies."""
        result = compute_stagger_offsets("bad cron", total_jobs=3, jitter_seconds=5)
        assert len(result) == 3
        for cron, jitter in result:
            assert cron == "bad cron"
            assert jitter == 5

    def test_more_customers_than_step_minutes(self):
        """More customers than minutes in step should wrap around."""
        result = compute_stagger_offsets("*/5 * * * *", total_jobs=15, jitter_seconds=5)
        assert len(result) == 15
        # All should have the step value 5
        for cron, _ in result:
            assert "/5" in cron

    def test_comma_minute_two_customers(self):
        """Comma-separated minutes should be offset evenly."""
        result = compute_stagger_offsets("0,30 * * * *", total_jobs=2, jitter_seconds=10)
        assert len(result) == 2
        # First: original
        assert result[0][0] == "0,30 * * * *"
        # Second: offset by frequency/2
        # Frequency is 30 minutes (0 and 30), offset 15
        second_mins = result[1][0].split(" ")[0]
        assert "15" in second_mins or "45" in second_mins

    def test_result_count_matches_total_jobs(self):
        """Result length should always match total_jobs."""
        for n in [1, 2, 5, 10, 50, 100]:
            result = compute_stagger_offsets("*/10 * * * *", total_jobs=n)
            assert len(result) == n
