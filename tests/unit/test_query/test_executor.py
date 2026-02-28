"""Unit tests for ViewExecutor - parameterized view execution."""

from unittest.mock import MagicMock

import pyarrow as pa
import pytest

from dfe_engine.query.catalog import ViewCatalog
from dfe_engine.query.executor import ViewExecutionError, ViewExecutor
from dfe_engine.query.models import (
    AuthContext,
    AuthorizationError,
    QueryOptions,
    ViewDefinition,
    ViewParameter,
)


def _make_view_def(
    name: str = "dfe_v_analytics_events",
    label: str = "analytics/events",
    params: list[ViewParameter] | None = None,
    tenant_isolated: bool = True,
    required_roles: list[str] | None = None,
) -> ViewDefinition:
    """Helper to create ViewDefinition for testing."""
    if params is None:
        params = [
            ViewParameter(
                name="org_id",
                clickhouse_type="String",
                python_type="string",
                reserved=True,
            ),
            ViewParameter(
                name="limit",
                clickhouse_type="UInt32",
                python_type="integer",
                typescript_type="number",
                input_type="number",
            ),
        ]
    parts = label.split("/", 1)
    namespace = parts[0]
    short_name = parts[1] if len(parts) > 1 else parts[0]
    return ViewDefinition(
        name=name,
        label=label,
        database="testdb",
        parameters=params,
        create_sql=f"CREATE VIEW {name} AS ...",
        namespace=namespace,
        short_name=short_name,
        tenant_isolated=tenant_isolated,
        required_roles=required_roles or [],
    )


def _make_auth(
    org_id: str = "tenant-123",
    user_id: str = "user-456",
    roles: list[str] | None = None,
) -> AuthContext:
    """Helper to create AuthContext for testing."""
    return AuthContext(
        org_id=org_id,
        user_id=user_id,
        roles=roles or [],
        request_id="req-001",
    )


def _make_arrow_table(num_rows: int = 5) -> pa.Table:
    """Create a simple Arrow table for mocking."""
    return pa.table({"id": list(range(num_rows)), "value": ["v"] * num_rows})


class TestViewExecutorSecurity:
    """Tests for security enforcement."""

    def test_org_id_always_injected(self):
        """Test that org_id is always injected from auth, not client params."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table()

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
        )

        auth = _make_auth(org_id="real-org")

        # Client tries to override org_id — should be ignored
        executor.execute(
            "analytics/events",
            params={"org_id": "hacker-org"},
            auth=auth,
        )

        # Verify the SQL was called with server-side binding params
        call_kwargs = client.query_arrow.call_args
        params_sent = call_kwargs.kwargs.get("parameters", call_kwargs[1].get("parameters", {}))
        assert params_sent["org_id"] == "real-org"

    def test_role_check_passes(self):
        """Test that role check passes when user has required role."""
        view_def = _make_view_def(required_roles=["analyst"])
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table()

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
        )

        auth = _make_auth(roles=["analyst"])
        result = executor.execute("analytics/events", params={}, auth=auth)
        assert result.num_rows == 5

    def test_role_check_fails(self):
        """Test that role check raises AuthorizationError."""
        view_def = _make_view_def(required_roles=["admin"])
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
        )

        auth = _make_auth(roles=["viewer"])
        with pytest.raises(AuthorizationError, match="requires one of roles"):
            executor.execute("analytics/events", params={}, auth=auth)

    def test_non_tenant_isolated_requires_admin(self):
        """Non-tenant-isolated views require admin role."""
        view_def = _make_view_def(tenant_isolated=False)
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
        )

        auth = _make_auth(roles=["viewer"])
        with pytest.raises(AuthorizationError, match="not tenant-isolated"):
            executor.execute("analytics/events", params={}, auth=auth)

    def test_non_tenant_isolated_admin_passes(self):
        """Admin can access non-tenant-isolated views."""
        view_def = _make_view_def(tenant_isolated=False)
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table()

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
        )

        auth = _make_auth(roles=["admin"])
        result = executor.execute("analytics/events", params={}, auth=auth)
        assert result.num_rows == 5


class TestViewExecutorLimits:
    """Tests for limit and timeout enforcement."""

    def test_limit_capped_at_max(self):
        """Test that client-requested limit is capped at max_limit."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table()

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
            max_limit=500,
        )

        auth = _make_auth(roles=["admin"])
        # QueryOptions validates le=100_000, so use a value within model bounds
        # but above the executor's max_limit of 500
        options = QueryOptions(limit=50000)

        executor.execute("analytics/events", params={}, auth=auth, options=options)

        # Verify the params include capped limit
        call_kwargs = client.query_arrow.call_args
        params_sent = call_kwargs.kwargs.get("parameters", call_kwargs[1].get("parameters", {}))
        assert params_sent["limit"] == 500

    def test_default_limit_applied(self):
        """Test that default limit is used when not specified."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table()

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
            default_limit=100,
        )

        auth = _make_auth(roles=["admin"])
        executor.execute("analytics/events", params={}, auth=auth)

        call_kwargs = client.query_arrow.call_args
        params_sent = call_kwargs.kwargs.get("parameters", call_kwargs[1].get("parameters", {}))
        assert params_sent["limit"] == 100

    def test_timeout_capped_at_max(self):
        """Test that timeout is capped at max_timeout."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table()

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
            max_timeout=60,
        )

        auth = _make_auth(roles=["admin"])
        # QueryOptions validates le=300, so use a value within model bounds
        # but above the executor's max_timeout of 60
        options = QueryOptions(timeout_seconds=200)

        executor.execute("analytics/events", params={}, auth=auth, options=options)

        call_kwargs = client.query_arrow.call_args
        settings_sent = call_kwargs.kwargs.get("settings", call_kwargs[1].get("settings", {}))
        assert settings_sent["max_execution_time"] == 60


class TestViewExecutorSQL:
    """Tests for SQL generation."""

    def test_basic_sql_generation(self):
        """Test SQL generation for a simple view call."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table()

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
        )

        auth = _make_auth(roles=["admin"])
        executor.execute("analytics/events", params={}, auth=auth)

        sql = client.query_arrow.call_args[0][0]
        # Should contain view call with parameterized syntax
        assert "testdb.dfe_v_analytics_events(" in sql
        assert "org_id={org_id:String}" in sql
        assert "limit={limit:UInt32}" in sql

    def test_offset_pagination_sql(self):
        """Test SQL with offset pagination wrapping."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table()

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
        )

        auth = _make_auth(roles=["admin"])
        options = QueryOptions(offset=50, limit=25)

        executor.execute("analytics/events", params={}, auth=auth, options=options)

        sql = client.query_arrow.call_args[0][0]
        assert "LIMIT 25 OFFSET 50" in sql

    def test_execution_error_wrapped(self):
        """Test that ClickHouse errors are wrapped in ViewExecutionError."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.side_effect = Exception("Connection refused")

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
        )

        auth = _make_auth(roles=["admin"])
        with pytest.raises(ViewExecutionError, match="Failed to execute"):
            executor.execute("analytics/events", params={}, auth=auth)


class TestViewExecutorMetadata:
    """Tests for response metadata."""

    def test_metadata_populated(self):
        """Test that result metadata is properly populated."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table(num_rows=10)

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
        )

        auth = _make_auth(roles=["admin"])
        result = executor.execute("analytics/events", params={}, auth=auth)

        assert result.metadata.row_count == 10
        assert result.metadata.query_label == "analytics/events"
        assert result.metadata.datasource == "clickhouse"
        assert result.metadata.store == "testdb"
        assert result.metadata.request_id == "req-001"

    def test_has_more_when_full(self):
        """Test has_more=True when result equals limit."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        # Return exactly default_limit rows
        client.query_arrow.return_value = _make_arrow_table(num_rows=100)

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
            default_limit=100,
        )

        auth = _make_auth(roles=["admin"])
        result = executor.execute("analytics/events", params={}, auth=auth)

        assert result.metadata.has_more is True
        assert result.metadata.next_offset == 100

    def test_has_more_false_when_partial(self):
        """Test has_more=False when result is less than limit."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query_arrow.return_value = _make_arrow_table(num_rows=50)

        executor = ViewExecutor(
            restricted_client=client,
            catalog=catalog,
            database="testdb",
            default_limit=100,
        )

        auth = _make_auth(roles=["admin"])
        result = executor.execute("analytics/events", params={}, auth=auth)

        assert result.metadata.has_more is False
        assert result.metadata.next_offset is None


class TestViewExecutorListing:
    """Tests for view listing delegation."""

    def test_list_views(self):
        """Test that list_views delegates to catalog."""
        catalog = MagicMock(spec=ViewCatalog)
        catalog.list_views.return_value = [_make_view_def()]

        executor = ViewExecutor(
            restricted_client=MagicMock(),
            catalog=catalog,
            database="testdb",
        )

        views = executor.list_views(namespace="analytics")
        catalog.list_views.assert_called_once_with(namespace="analytics")
        assert len(views) == 1

    def test_get_view(self):
        """Test that get_view delegates to catalog."""
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = _make_view_def()

        executor = ViewExecutor(
            restricted_client=MagicMock(),
            catalog=catalog,
            database="testdb",
        )

        view = executor.get_view("analytics/events")
        catalog.get_view.assert_called_once_with("analytics/events")
        assert view.label == "analytics/events"
