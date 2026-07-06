"""Unit tests for ViewExecutor — parameterized view execution."""

from unittest.mock import MagicMock

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
    if params is None:
        params = [
            ViewParameter(
                name="org_id", clickhouse_type="String", python_type="string", reserved=True
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
    """Build AuthContext. Include a builtin role with query:execute when auth is enabled."""
    return AuthContext(org_id=org_id, user_id=user_id, roles=roles or [], request_id="req-001")


def _roles_with_query_execute(*extra: str) -> list[str]:
    """Roles that pass authorize(..., 'query:execute') with builtin roles.yaml."""
    return ["data_analyst", *extra]


def _make_query_result(num_rows: int = 5):
    """Create a mock clickhouse-connect query result."""
    result = MagicMock()
    result.column_names = ["id", "value"]
    result.result_rows = [(i, "v") for i in range(num_rows)]
    return result


class TestViewExecutorSecurity:
    def test_org_id_always_injected(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(org_id="real-org", roles=_roles_with_query_execute())

        executor.execute("analytics/events", params={"org_id": "hacker-org"}, auth=auth)

        call_kwargs = client.query.call_args
        params_sent = call_kwargs.kwargs.get("parameters", call_kwargs[1].get("parameters", {}))
        assert params_sent["org_id"] == "real-org"

    def test_role_check_passes(self):
        view_def = _make_view_def(required_roles=["analyst"])
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=_roles_with_query_execute("analyst"))
        result = executor.execute("analytics/events", params={}, auth=auth)
        assert result.num_rows == 5

    def test_role_check_fails(self):
        view_def = _make_view_def(required_roles=["admin"])
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=_roles_with_query_execute("viewer"))
        with pytest.raises(AuthorizationError, match="requires one of roles"):
            executor.execute("analytics/events", params={}, auth=auth)

    def test_non_tenant_isolated_requires_admin(self):
        view_def = _make_view_def(tenant_isolated=False)
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["data_viewer"])
        with pytest.raises(AuthorizationError, match="not tenant-isolated"):
            executor.execute("analytics/events", params={}, auth=auth)

    def test_non_tenant_isolated_admin_passes(self):
        view_def = _make_view_def(tenant_isolated=False)
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        result = executor.execute("analytics/events", params={}, auth=auth)
        assert result.num_rows == 5


class TestViewExecutorLimits:
    def test_limit_capped_at_max(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(
            restricted_client=client, catalog=catalog, database="testdb", max_limit=500
        )
        auth = _make_auth(roles=["admin"])
        options = QueryOptions(limit=50000)
        executor.execute("analytics/events", params={}, auth=auth, options=options)

        call_kwargs = client.query.call_args
        params_sent = call_kwargs.kwargs.get("parameters", call_kwargs[1].get("parameters", {}))
        assert params_sent["limit"] == 500

    def test_default_limit_applied(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(
            restricted_client=client, catalog=catalog, database="testdb", default_limit=100
        )
        auth = _make_auth(roles=["admin"])
        executor.execute("analytics/events", params={}, auth=auth)

        call_kwargs = client.query.call_args
        params_sent = call_kwargs.kwargs.get("parameters", call_kwargs[1].get("parameters", {}))
        assert params_sent["limit"] == 100

    def test_timeout_capped_at_max(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(
            restricted_client=client, catalog=catalog, database="testdb", max_timeout=60
        )
        auth = _make_auth(roles=["admin"])
        options = QueryOptions(timeout_seconds=200)
        executor.execute("analytics/events", params={}, auth=auth, options=options)

        call_kwargs = client.query.call_args
        settings_sent = call_kwargs.kwargs.get("settings", call_kwargs[1].get("settings", {}))
        assert settings_sent["max_execution_time"] == 60


class TestViewExecutorSQL:
    def test_basic_sql_generation(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        executor.execute("analytics/events", params={}, auth=auth)

        sql = client.query.call_args[0][0]
        assert "testdb.dfe_v_analytics_events(" in sql
        assert "org_id={org_id:String}" in sql
        assert "limit={limit:UInt32}" in sql

    def test_offset_pagination_sql(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        options = QueryOptions(offset=50, limit=25)
        executor.execute("analytics/events", params={}, auth=auth, options=options)

        sql = client.query.call_args[0][0]
        assert "LIMIT 25 OFFSET 50" in sql

    def test_offset_pagination_inner_limit_covers_window(self):
        """Page 2+ of a view with a declared limit param: the inner view limit
        must be bound to offset + page size, or the outer LIMIT/OFFSET slices
        an already-truncated inner result and the page comes back empty."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        options = QueryOptions(offset=50, limit=25)
        executor.execute("analytics/events", params={}, auth=auth, options=options)

        call_kwargs = client.query.call_args
        sql = call_kwargs[0][0]
        params_sent = call_kwargs.kwargs.get("parameters", call_kwargs[1].get("parameters", {}))
        assert params_sent["limit"] == 75  # inner window = offset + page size
        assert "LIMIT 25 OFFSET 50" in sql  # outer slice is still the page

    def test_first_page_inner_limit_is_page_size(self):
        """offset=0 keeps the inner limit at the page size (no outer clause)."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        options = QueryOptions(offset=0, limit=25)
        executor.execute("analytics/events", params={}, auth=auth, options=options)

        call_kwargs = client.query.call_args
        params_sent = call_kwargs.kwargs.get("parameters", call_kwargs[1].get("parameters", {}))
        assert params_sent["limit"] == 25

    def test_zero_param_view_emits_no_parens(self):
        """P1.2: a view with no parameters is a plain CH view, not a table
        function - emit 'FROM db.view', never 'db.view()' (Code 46 otherwise)."""
        view_def = _make_view_def(
            name="dfe_v_overview_active_sources",
            label="overview/active_sources",
            params=[],
            tenant_isolated=False,
        )
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        executor.execute("overview/active_sources", params={}, auth=auth)

        sql = client.query.call_args[0][0]
        assert "FROM testdb.dfe_v_overview_active_sources" in sql
        assert "dfe_v_overview_active_sources(" not in sql  # no table-function parens

    def test_offset_inner_limit_capped_at_max(self):
        """P3.13: a huge offset must not bypass max_limit on the inner scan."""
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(
            restricted_client=client, catalog=catalog, database="testdb", max_limit=100_000
        )
        auth = _make_auth(roles=["admin"])
        options = QueryOptions(offset=10**9, limit=1000)
        executor.execute("analytics/events", params={}, auth=auth, options=options)

        call_kwargs = client.query.call_args
        params_sent = call_kwargs.kwargs.get("parameters", call_kwargs[1].get("parameters", {}))
        assert params_sent["limit"] == 100_000  # capped, not 10**9 + 1000

    def test_execution_error_wrapped(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.side_effect = Exception("Connection refused")

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        with pytest.raises(ViewExecutionError, match="Failed to execute"):
            executor.execute("analytics/events", params={}, auth=auth)


class TestViewExecutorKeyset:
    """Keyset pagination: order_by must be allowlisted, after_key must be bound.

    Regression for F-QUERY-ORDERBY - a free-string order_by was interpolated raw
    into the outer wrapper query (SQL injection on the tenant-isolated executor),
    and after_key emitted a bare, never-bound {_after_key} placeholder.
    """

    def test_order_by_injection_rejected(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        options = QueryOptions(after_key=1, order_by="1 UNION ALL SELECT * FROM dfe.landing -- ")
        with pytest.raises(ViewExecutionError, match="Invalid order_by"):
            executor.execute("analytics/events", params={}, auth=auth, options=options)
        # The injection is rejected BEFORE any SQL reaches ClickHouse.
        client.query.assert_not_called()

    def test_order_by_quoted_or_dotted_rejected(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        for bad in ("id; DROP TABLE t", "`id`", "id, secret", "col DESC"):
            options = QueryOptions(after_key=1, order_by=bad)
            with pytest.raises(ViewExecutionError, match="Invalid order_by"):
                executor.execute("analytics/events", params={}, auth=auth, options=options)
        client.query.assert_not_called()

    def test_valid_order_by_emits_and_binds_after_key(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        options = QueryOptions(after_key=100, order_by="created_at", order_dir="asc")
        executor.execute("analytics/events", params={}, auth=auth, options=options)

        call = client.query.call_args
        sql = call[0][0]
        params_sent = call.kwargs.get("parameters", call[1].get("parameters", {}))
        # order_by is interpolated as a bare identifier; after_key is a real
        # server-side bound parameter (typed), not a literal.
        assert "WHERE created_at > {_after_key:Int64}" in sql
        assert "ORDER BY created_at asc" in sql
        assert params_sent["_after_key"] == 100

    def test_keyset_desc_uses_lt_operator(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result()

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        options = QueryOptions(after_key="2026-01-01", order_by="ts", order_dir="desc")
        executor.execute("analytics/events", params={}, auth=auth, options=options)

        sql = client.query.call_args[0][0]
        # String cursor binds as String; desc uses the < operator.
        assert "WHERE ts < {_after_key:String}" in sql
        assert "ORDER BY ts desc" in sql


class TestViewExecutorMetadata:
    def test_metadata_populated(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result(num_rows=10)

        executor = ViewExecutor(restricted_client=client, catalog=catalog, database="testdb")
        auth = _make_auth(roles=["admin"])
        result = executor.execute("analytics/events", params={}, auth=auth)

        assert result.metadata.row_count == 10
        assert result.metadata.query_label == "analytics/events"
        assert result.metadata.datasource == "clickhouse"
        assert result.metadata.store == "testdb"
        assert result.metadata.request_id == "req-001"

    def test_has_more_when_full(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result(num_rows=100)

        executor = ViewExecutor(
            restricted_client=client, catalog=catalog, database="testdb", default_limit=100
        )
        auth = _make_auth(roles=["admin"])
        result = executor.execute("analytics/events", params={}, auth=auth)

        assert result.metadata.has_more is True
        assert result.metadata.next_offset == 100

    def test_has_more_false_when_partial(self):
        view_def = _make_view_def()
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = view_def

        client = MagicMock()
        client.query.return_value = _make_query_result(num_rows=50)

        executor = ViewExecutor(
            restricted_client=client, catalog=catalog, database="testdb", default_limit=100
        )
        auth = _make_auth(roles=["admin"])
        result = executor.execute("analytics/events", params={}, auth=auth)

        assert result.metadata.has_more is False
        assert result.metadata.next_offset is None


class TestViewExecutorListing:
    def test_list_views(self):
        catalog = MagicMock(spec=ViewCatalog)
        catalog.list_views.return_value = [_make_view_def()]

        executor = ViewExecutor(restricted_client=MagicMock(), catalog=catalog, database="testdb")
        views = executor.list_views(namespace="analytics")
        catalog.list_views.assert_called_once_with(namespace="analytics")
        assert len(views) == 1

    def test_get_view(self):
        catalog = MagicMock(spec=ViewCatalog)
        catalog.get_view.return_value = _make_view_def()

        executor = ViewExecutor(restricted_client=MagicMock(), catalog=catalog, database="testdb")
        view = executor.get_view("analytics/events")
        assert view.label == "analytics/events"
