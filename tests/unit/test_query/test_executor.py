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


class TestViewExecutorOrderByInjection:
    """order_by is interpolated into ORDER BY/WHERE (CH cannot bind an identifier),
    so it MUST be a bare column identifier - a security regression guard."""

    def test_order_by_injection_rejected(self):
        catalog = MagicMock(spec=ViewCatalog)
        executor = ViewExecutor(restricted_client=MagicMock(), catalog=catalog, database="testdb")
        options = QueryOptions(order_by="ts) UNION SELECT * FROM secrets --", after_key="k")
        with pytest.raises(ViewExecutionError):
            executor._build_sql(_make_view_def(), {"org_id": "t"}, 10, 0, options)

    def test_order_by_valid_identifier_allowed(self):
        catalog = MagicMock(spec=ViewCatalog)
        executor = ViewExecutor(restricted_client=MagicMock(), catalog=catalog, database="testdb")
        options = QueryOptions(order_by="event_ts", after_key="k", order_dir="asc")
        sql = executor._build_sql(_make_view_def(), {"org_id": "t"}, 10, 0, options)
        assert "ORDER BY event_ts asc" in sql
