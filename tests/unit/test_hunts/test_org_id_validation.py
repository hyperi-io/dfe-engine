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

# Values that must all be treated as "no org id" and refused.
BLANK_ORG_IDS = ["", "   ", "\t", "\n", None]


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
