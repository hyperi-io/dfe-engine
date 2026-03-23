"""Unit tests for parameterized view models."""

from dfe_engine.query.models import (
    QueryOptions,
    ViewDefinition,
    ViewExecuteRequest,
    ViewParameter,
)


class TestViewParameter:
    """Tests for ViewParameter model."""

    def test_minimal_parameter(self):
        """Test creating a parameter with required fields only."""
        param = ViewParameter(
            name="org_id",
            clickhouse_type="String",
            python_type="string",
        )
        assert param.name == "org_id"
        assert param.clickhouse_type == "String"
        assert param.python_type == "string"
        assert param.typescript_type == "string"
        assert param.input_type == "text"
        assert param.required is True
        assert param.reserved is False
        assert param.description is None
        assert param.placeholder is None
        assert param.enum is None

    def test_full_parameter(self):
        """Test creating a parameter with all fields."""
        param = ViewParameter(
            name="severity",
            clickhouse_type="String",
            python_type="string",
            typescript_type="string",
            input_type="select",
            required=True,
            reserved=False,
            description="Alert severity level",
            placeholder="critical",
            enum=["low", "medium", "high", "critical"],
        )
        assert param.name == "severity"
        assert param.input_type == "select"
        assert param.description == "Alert severity level"
        assert param.enum == ["low", "medium", "high", "critical"]

    def test_reserved_parameter(self):
        """Test reserved parameter (injected server-side)."""
        param = ViewParameter(
            name="org_id",
            clickhouse_type="String",
            python_type="string",
            reserved=True,
        )
        assert param.reserved is True

    def test_array_parameter(self):
        """Test array parameter with TypeScript type."""
        param = ViewParameter(
            name="event_types",
            clickhouse_type="Array(String)",
            python_type="array",
            typescript_type="string[]",
            input_type="multiselect",
        )
        assert param.typescript_type == "string[]"
        assert param.input_type == "multiselect"

    def test_serialization_roundtrip(self):
        """Test JSON serialization and deserialization."""
        param = ViewParameter(
            name="limit",
            clickhouse_type="UInt32",
            python_type="integer",
            typescript_type="number",
            input_type="number",
        )
        data = param.model_dump()
        restored = ViewParameter(**data)
        assert restored == param


class TestViewDefinition:
    """Tests for ViewDefinition model."""

    def test_minimal_definition(self):
        """Test creating a view definition with required fields."""
        view = ViewDefinition(
            name="dfe_v_system_health",
            label="system/health",
            database="default",
            create_sql="CREATE VIEW dfe_v_system_health AS SELECT 1",
            namespace="system",
            short_name="health",
        )
        assert view.name == "dfe_v_system_health"
        assert view.label == "system/health"
        assert view.database == "default"
        assert view.namespace == "system"
        assert view.short_name == "health"
        assert view.parameters == []
        assert view.tenant_isolated is True
        assert view.required_roles == []

    def test_definition_with_parameters(self):
        """Test view definition with parameters."""
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
        view = ViewDefinition(
            name="dfe_v_analytics_events",
            label="analytics/events",
            database="prod",
            parameters=params,
            create_sql="CREATE VIEW ...",
            namespace="analytics",
            short_name="events",
        )
        assert len(view.parameters) == 2
        assert view.parameters[0].name == "org_id"
        assert view.parameters[0].reserved is True

    def test_definition_with_roles(self):
        """Test view definition with required roles."""
        view = ViewDefinition(
            name="dfe_v_admin_users",
            label="admin/users",
            database="default",
            create_sql="CREATE VIEW ...",
            namespace="admin",
            short_name="users",
            required_roles=["admin", "superadmin"],
        )
        assert view.required_roles == ["admin", "superadmin"]

    def test_non_tenant_isolated(self):
        """Test non-tenant-isolated view."""
        view = ViewDefinition(
            name="dfe_v_system_global",
            label="system/global",
            database="default",
            create_sql="CREATE VIEW ...",
            namespace="system",
            short_name="global",
            tenant_isolated=False,
        )
        assert view.tenant_isolated is False

    def test_serialization_roundtrip(self):
        """Test full roundtrip serialization."""
        view = ViewDefinition(
            name="dfe_v_analytics_events",
            label="analytics/events",
            database="default",
            parameters=[
                ViewParameter(
                    name="org_id",
                    clickhouse_type="String",
                    python_type="string",
                    reserved=True,
                ),
            ],
            create_sql="CREATE VIEW dfe_v_analytics_events AS ...",
            modified_at="2026-01-15 10:00:00",
            namespace="analytics",
            short_name="events",
            description="Event analytics view",
        )
        data = view.model_dump()
        restored = ViewDefinition(**data)
        assert restored == view


class TestViewExecuteRequest:
    """Tests for ViewExecuteRequest model."""

    def test_empty_request(self):
        """Test request with no params."""
        req = ViewExecuteRequest()
        assert req.params == {}
        assert req.options is None

    def test_request_with_params(self):
        """Test request with parameters."""
        req = ViewExecuteRequest(
            params={"event_type": "login", "limit": 100},
        )
        assert req.params["event_type"] == "login"
        assert req.params["limit"] == 100

    def test_request_with_options(self):
        """Test request with query options."""
        req = ViewExecuteRequest(
            params={"severity": "high"},
            options=QueryOptions(limit=500, offset=0),
        )
        assert req.options is not None
        assert req.options.limit == 500
