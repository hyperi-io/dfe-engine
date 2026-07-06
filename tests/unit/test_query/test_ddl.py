"""Unit tests for DDLManager - parameterized-view management.

The restricted-reader RBAC moved to the query_reader service role (reconciled by
governance.ch.ChRbacReconciler); DDLManager only manages views + grants SELECT
to dfe_query_reader_role.
"""

from unittest.mock import MagicMock

import pytest

from dfe_engine.query.ddl import DDLManager


class TestViewManagement:
    """Tests for view creation, granting, and dropping."""

    def test_apply_view(self):
        """Test applying a single view."""
        client = MagicMock()
        mgr = DDLManager(client=client, database="testdb")

        sql = "CREATE OR REPLACE VIEW dfe_v_system_health AS SELECT 1"
        mgr.apply_view("dfe_v_system_health", sql)

        # Should execute CREATE and GRANT. The CREATE head is qualified with the
        # target db at apply time (P2.11) so the view lands where the catalog scans.
        assert client.command.call_count == 2
        calls = [c.args[0] for c in client.command.call_args_list]
        assert calls[0] == "CREATE OR REPLACE VIEW testdb.dfe_v_system_health AS SELECT 1"
        assert "GRANT SELECT ON testdb.dfe_v_system_health TO dfe_query_reader_role" in calls[1]

    def test_apply_view_invalid_prefix(self):
        """Test that apply_view rejects views without dfe_v_ prefix."""
        client = MagicMock()
        mgr = DDLManager(client=client)

        with pytest.raises(ValueError, match="must start with"):
            mgr.apply_view("bad_name", "CREATE VIEW ...")

    def test_drop_view(self):
        """Test dropping a view."""
        client = MagicMock()
        mgr = DDLManager(client=client, database="testdb")

        mgr.drop_view("dfe_v_system_health")

        # Should revoke and drop
        assert client.command.call_count == 2
        calls = [c.args[0] for c in client.command.call_args_list]
        assert "REVOKE SELECT" in calls[0]
        assert "DROP VIEW IF EXISTS testdb.dfe_v_system_health" in calls[1]


class TestBuiltinViews:
    """Tests for builtin view discovery and application."""

    def test_apply_all_builtin_views(self):
        """Test applying all .sql files from builtin_views directory."""
        client = MagicMock()
        mgr = DDLManager(client=client, database="default")

        applied = mgr.apply_all_builtin_views()

        # Should find the builtin .sql files (system + overview views)
        assert len(applied) >= 3
        assert "dfe_v_system_health" in applied
        assert "dfe_v_system_table_sizes" in applied
        assert "dfe_v_overview_active_sources" in applied

    def test_apply_view_qualifies_head_and_substitutes_db(self):
        """P2.11 + P2.12: the CREATE head is qualified with the target data db (so
        the catalog scanning that db discovers the view), and a {db} token in the
        body resolves to the same db (so {db}.hunt_schedule matches where the
        runner writes), instead of a hardcoded database."""
        client = MagicMock()
        mgr = DDLManager(client=client, database="dfe_data")
        mgr.apply_view(
            "dfe_v_test",
            "CREATE OR REPLACE VIEW dfe_v_test AS SELECT * FROM {db}.hunt_state",
        )
        executed = client.command.call_args_list[0][0][0]
        assert "CREATE OR REPLACE VIEW dfe_data.dfe_v_test AS" in executed
        assert "FROM dfe_data.hunt_state" in executed
        assert "{db}" not in executed

    def test_diff_views(self):
        """Test diffing builtin vs live views."""
        client = MagicMock()
        result = MagicMock()
        result.result_rows = [
            ("dfe_v_system_health",),
            ("dfe_v_custom_extra",),
        ]
        client.query.return_value = result

        mgr = DDLManager(client=client, database="default")
        diff = mgr.diff_views()

        # dfe_v_system_health should be in "present"
        assert "dfe_v_system_health" in diff["present"]

        # dfe_v_custom_extra is live but not in builtin files
        assert "dfe_v_custom_extra" in diff["extra"]

        # dfe_v_system_table_sizes and the overview views are in files but not live
        assert "dfe_v_system_table_sizes" in diff["missing"]
        assert "dfe_v_overview_active_sources" in diff["missing"]


class TestBootstrap:
    """Tests for full bootstrap flow."""

    def test_bootstrap(self):
        """Test bootstrap applies the builtin views (reader RBAC is elsewhere now)."""
        client = MagicMock()
        mgr = DDLManager(client=client, database="default")

        applied = mgr.bootstrap()

        # 2 per view (CREATE + GRANT) x 3 views = 6
        assert client.command.call_count >= 6
        assert len(applied) >= 3
