"""Edge-dimension tests for the _org_id (tenant isolation) write-guards.

Phase E renamed the state-table key `customer_name` -> `_org_id` and added a
non-empty guard on every write path: a blank org id must never be written into
the isolation column (it would create rows that belong to "no tenant" and leak
across the restrictive row policies). These tests drive the deliberate bad
inputs - empty string, None, whitespace-only - not the happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from dfe_engine.hunts.alert_grouping import AlertStateManager
from dfe_engine.hunts.checkpoint import HuntCheckpointManager

# Values that must all be treated as "no org id" and refused.
BLANK_ORG_IDS = ["", "   ", "\t", "\n", None]


def _single_checkpoint_kwargs(customer) -> dict:
    """Full kwargs for create_checkpoint_clickhouse with valid-format timestamps.

    Timestamps are deliberately valid so that if the empty-org guard ever
    regressed, the call would proceed (and the assertion on execute would fail)
    rather than blowing up on timestamp parsing for an unrelated reason.
    """
    ts = "2026-03-01 00:00:00"
    return {
        "customer": customer,
        "rule": "rule_1",
        "hunt_name": "hunt_1",
        "query_id": "q-1",
        "log_buffer": 60,
        "query_schedule_time_str": ts,
        "execution_time_str": ts,
        "end_time_str": ts,
        "previous_successful_checkpoint_str": ts,
        "query_checkpoint_time_str": ts,
        "execution_time_ms": 10,
        "thread_id": "t1",
    }


def _batch_checkpoint(org_id) -> dict:
    ts = "2026-03-01 00:00:00"
    return {
        "_org_id": org_id,
        "rule_name": "rule_1",
        "thread_id": "t1",
        "log_buffer": 10,
        "query_schedule_time": ts,
        "execution_time": ts,
        "end_time": ts,
        "previous_successful_checkpoint": ts,
        "query_checkpoint_time": ts,
        "execution_time_ms": 1500,
        "hunt_name": "hunt_1",
        "query_id": "q-1",
    }


# ── AlertStateManager.record_fire ────────────────────────────────


class TestRecordFireOrgGuard:
    @pytest.mark.parametrize("org", BLANK_ORG_IDS)
    def test_blank_org_is_refused(self, org):
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch = MagicMock()
        mgr.record_fire(ch, "hunt_1", "rule_1", org, fired_at=datetime(2026, 3, 3, tzinfo=UTC))
        ch.execute.assert_not_called()

    def test_valid_org_is_written(self):
        mgr = AlertStateManager()
        mgr._table_ensured = True
        ch = MagicMock()
        mgr.record_fire(ch, "hunt_1", "rule_1", "acme", fired_at=datetime(2026, 3, 3, tzinfo=UTC))
        ch.execute.assert_called_once()
        # Data is a POSITIONAL arg (routes to insert()); _org_id is column index 2.
        data = ch.execute.call_args[0][1]
        assert data[0][2] == "acme"


# ── HuntCheckpointManager single ClickHouse insert ───────────────


class TestCheckpointClickhouseOrgGuard:
    @pytest.mark.parametrize("org", BLANK_ORG_IDS)
    def test_blank_org_is_refused(self, org):
        mgr = HuntCheckpointManager()
        ch = MagicMock()
        mgr.create_checkpoint_clickhouse(ch_client=ch, **_single_checkpoint_kwargs(org))
        ch.execute.assert_not_called()

    def test_valid_org_is_written(self):
        mgr = HuntCheckpointManager()
        ch = MagicMock()
        mgr.create_checkpoint_clickhouse(ch_client=ch, **_single_checkpoint_kwargs("acme"))
        ch.execute.assert_called_once()
        data = ch.execute.call_args[0][1]
        assert data[0][0] == "acme"  # _org_id is the first column


# ── HuntCheckpointManager file checkpoint ────────────────────────


class TestCheckpointFileOrgGuard:
    @pytest.mark.parametrize("org", BLANK_ORG_IDS)
    def test_blank_org_writes_no_file(self, org, tmp_path):
        mgr = HuntCheckpointManager()
        fp = tmp_path / "checkpoints.json"
        mgr.create_checkpoint_file(
            customer=org,
            rule="rule_1",
            hunt_name="hunt_1",
            query_id="q-1",
            thread_id="t1",
            log_buffer=60,
            previous_successful_checkpoint_str="2026-03-01 00:00:00",
            query_schedule_time_str="2026-03-01 00:00:00",
            execution_time_str="2026-03-01 00:00:00",
            end_time_str="2026-03-01 00:00:00",
            query_checkpoint_time_str="2026-03-01 00:00:00",
            execution_time_ms=10,
            file_path=str(fp),
        )
        assert not fp.exists()


# ── HuntCheckpointManager batch insert ───────────────────────────


class TestBatchCheckpointOrgGuard:
    def test_all_blank_org_inserts_nothing(self):
        mgr = HuntCheckpointManager()
        ch = MagicMock()
        mgr.create_batch_checkpoint_clickhouse(
            ch, [_batch_checkpoint(""), _batch_checkpoint("   "), _batch_checkpoint(None)]
        )
        ch.execute.assert_not_called()

    def test_mixed_batch_keeps_only_valid_rows(self):
        mgr = HuntCheckpointManager()
        ch = MagicMock()
        mgr.create_batch_checkpoint_clickhouse(
            ch,
            [
                _batch_checkpoint(""),  # skipped
                _batch_checkpoint("acme"),  # kept
                _batch_checkpoint("   "),  # skipped
                _batch_checkpoint("globex"),  # kept
            ],
        )
        ch.execute.assert_called_once()
        data = ch.execute.call_args[0][1]
        assert len(data) == 2
        assert [row[0] for row in data] == ["acme", "globex"]
