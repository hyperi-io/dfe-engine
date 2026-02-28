"""Unit tests for DDLManager - RBAC bootstrap and view management."""

from unittest.mock import MagicMock

import pytest

from dfe_engine.query.ddl import DDLManager


class TestRBACBootstrap:
    """Tests for RBAC DDL generation."""

    def test_rbac_statements_default(self):
        """Test RBAC DDL with default settings."""
        client = MagicMock()
        mgr = DDLManager(client=client, database="default")

        stmts = mgr._build_rbac_statements()
        assert len(stmts) == 3

        # Settings profile
        assert "CREATE SETTINGS PROFILE IF NOT EXISTS dfe_query_profile" in stmts[0]
        assert "readonly = 1 CONST" in stmts[0]
        assert "max_execution_time = 30 CONST" in stmts[0]
        assert "max_rows_to_read = 10000000 CONST" in stmts[0]
        assert "max_memory_usage = '2G' CONST" in stmts[0]
        assert "allow_ddl = 0 CONST" in stmts[0]

        # Role
        assert stmts[1] == "CREATE ROLE IF NOT EXISTS dfe_query_reader"

        # User
        assert "CREATE USER IF NOT EXISTS dfe_query_user" in stmts[2]
        assert "SETTINGS PROFILE dfe_query_profile" in stmts[2]

    def test_rbac_statements_custom_limits(self):
        """Test RBAC DDL with custom resource limits."""
        client = MagicMock()
        mgr = DDLManager(
            client=client,
            database="prod",
            max_execution_time=60,
            max_rows_to_read=50_000_000,
            max_memory_usage="4G",
        )

        stmts = mgr._build_rbac_statements()
        assert "max_execution_time = 60 CONST" in stmts[0]
        assert "max_rows_to_read = 50000000 CONST" in stmts[0]
        assert "max_memory_usage = '4G' CONST" in stmts[0]

    def test_rbac_statements_custom_user(self):
        """Test RBAC DDL with custom username and password."""
        client = MagicMock()
        mgr = DDLManager(
            client=client,
            restricted_user="custom_reader",
            restricted_password="s3cret",
        )

        stmts = mgr._build_rbac_statements()
        assert "CREATE USER IF NOT EXISTS custom_reader" in stmts[2]
        assert "BY 's3cret'" in stmts[2]

    def test_ensure_rbac_executes_all(self):
        """Test that ensure_rbac executes all statements."""
        client = MagicMock()
        mgr = DDLManager(client=client)
        mgr.ensure_rbac()

        assert client.command.call_count == 3

    def test_ensure_rbac_raises_on_failure(self):
        """Test that ensure_rbac raises if a statement fails."""
        client = MagicMock()
        client.command.side_effect = Exception("Access denied")
        mgr = DDLManager(client=client)

        with pytest.raises(Exception, match="Access denied"):
            mgr.ensure_rbac()


class TestViewManagement:
    """Tests for view creation, granting, and dropping."""

    def test_apply_view(self):
        """Test applying a single view."""
        client = MagicMock()
        mgr = DDLManager(client=client, database="testdb")

        sql = "CREATE OR REPLACE VIEW dfe_v_system_health AS SELECT 1"
        mgr.apply_view("dfe_v_system_health", sql)

        # Should execute CREATE and GRANT
        assert client.command.call_count == 2
        calls = [c.args[0] for c in client.command.call_args_list]
        assert calls[0] == sql
        assert "GRANT SELECT ON testdb.dfe_v_system_health TO dfe_query_reader" in calls[1]

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

        # Should find the 3 builtin .sql files
        assert len(applied) >= 3
        assert "dfe_v_system_health" in applied
        assert "dfe_v_system_table_sizes" in applied
        assert "dfe_v_analytics_event_counts" in applied

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

        # dfe_v_system_table_sizes and dfe_v_analytics_event_counts are in files but not live
        assert "dfe_v_system_table_sizes" in diff["missing"]
        assert "dfe_v_analytics_event_counts" in diff["missing"]


class TestBootstrap:
    """Tests for full bootstrap flow."""

    def test_bootstrap(self):
        """Test bootstrap runs RBAC then applies views."""
        client = MagicMock()
        mgr = DDLManager(client=client, database="default")

        applied = mgr.bootstrap()

        # 3 RBAC statements + 2 per view (CREATE + GRANT) × 3 views = 9
        assert client.command.call_count >= 9
        assert len(applied) >= 3
