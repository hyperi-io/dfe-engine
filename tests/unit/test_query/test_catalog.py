"""Unit tests for ViewCatalog - view discovery and parameter parsing."""

from unittest.mock import MagicMock

import pytest

from dfe_engine.query.catalog import (
    ViewCatalog,
    label_to_view_name,
    map_clickhouse_type,
    parse_view_parameters,
    view_name_to_label,
)


# =============================================================================
# Type Mapping Tests
# =============================================================================


class TestMapClickHouseType:
    """Tests for ClickHouse → Python/TypeScript/HTML type mapping."""

    def test_string(self):
        assert map_clickhouse_type("String") == ("string", "string", "text")

    def test_uint64(self):
        assert map_clickhouse_type("UInt64") == ("integer", "number", "number")

    def test_uint32(self):
        assert map_clickhouse_type("UInt32") == ("integer", "number", "number")

    def test_uint8(self):
        assert map_clickhouse_type("UInt8") == ("integer", "number", "number")

    def test_int32(self):
        assert map_clickhouse_type("Int32") == ("integer", "number", "number")

    def test_int64(self):
        assert map_clickhouse_type("Int64") == ("integer", "number", "number")

    def test_float32(self):
        assert map_clickhouse_type("Float32") == ("float", "number", "number")

    def test_float64(self):
        assert map_clickhouse_type("Float64") == ("float", "number", "number")

    def test_decimal(self):
        assert map_clickhouse_type("Decimal(10,2)") == ("float", "number", "number")

    def test_datetime(self):
        assert map_clickhouse_type("DateTime") == ("datetime", "string", "datetime-local")

    def test_datetime64(self):
        assert map_clickhouse_type("DateTime64(3)") == (
            "datetime",
            "string",
            "datetime-local",
        )

    def test_date(self):
        assert map_clickhouse_type("Date") == ("date", "string", "date")

    def test_date32(self):
        assert map_clickhouse_type("Date32") == ("date", "string", "date")

    def test_uuid(self):
        assert map_clickhouse_type("UUID") == ("uuid", "string", "text")

    def test_bool(self):
        assert map_clickhouse_type("Bool") == ("boolean", "boolean", "checkbox")

    def test_array_string(self):
        assert map_clickhouse_type("Array(String)") == (
            "array",
            "string[]",
            "multiselect",
        )

    def test_array_uint64(self):
        assert map_clickhouse_type("Array(UInt64)") == (
            "array",
            "number[]",
            "multiselect",
        )

    def test_unknown_type_fallback(self):
        """Unknown types should fall back to string."""
        assert map_clickhouse_type("LowCardinality(String)") == (
            "string",
            "string",
            "text",
        )


# =============================================================================
# Parameter Parsing Tests
# =============================================================================


class TestParseViewParameters:
    """Tests for parsing {param:Type} patterns from CREATE VIEW SQL."""

    def test_simple_parameters(self):
        """Test parsing simple string and integer parameters."""
        sql = """
        CREATE VIEW dfe_v_test AS
        SELECT * FROM events
        WHERE org_id = {org_id:String}
        LIMIT {limit:UInt32}
        """
        params = parse_view_parameters(sql)
        assert len(params) == 2

        assert params[0].name == "org_id"
        assert params[0].clickhouse_type == "String"
        assert params[0].python_type == "string"
        assert params[0].reserved is True  # org_id is reserved

        assert params[1].name == "limit"
        assert params[1].clickhouse_type == "UInt32"
        assert params[1].python_type == "integer"
        assert params[1].reserved is False

    def test_datetime64_parameter(self):
        """Test parsing DateTime64 with precision."""
        sql = """
        CREATE VIEW dfe_v_test AS
        SELECT * FROM events
        WHERE timestamp >= {time_from:DateTime64(3)}
        AND timestamp < {time_to:DateTime64(3)}
        """
        params = parse_view_parameters(sql)
        assert len(params) == 2
        assert params[0].name == "time_from"
        assert params[0].clickhouse_type == "DateTime64(3)"
        assert params[0].python_type == "datetime"
        assert params[0].input_type == "datetime-local"

    def test_array_parameter(self):
        """Test parsing Array type parameter."""
        sql = """
        CREATE VIEW dfe_v_test AS
        SELECT * FROM events
        WHERE event_type IN {types:Array(String)}
        """
        params = parse_view_parameters(sql)
        assert len(params) == 1
        assert params[0].name == "types"
        assert params[0].clickhouse_type == "Array(String)"
        assert params[0].python_type == "array"
        assert params[0].typescript_type == "string[]"
        assert params[0].input_type == "multiselect"

    def test_deduplication(self):
        """Test that duplicate parameter names are deduplicated."""
        sql = """
        CREATE VIEW dfe_v_test AS
        SELECT * FROM events
        WHERE org_id = {org_id:String}
        UNION ALL
        SELECT * FROM other_events
        WHERE org_id = {org_id:String}
        LIMIT {limit:UInt32}
        """
        params = parse_view_parameters(sql)
        assert len(params) == 2
        names = [p.name for p in params]
        assert names == ["org_id", "limit"]

    def test_no_parameters(self):
        """Test view with no parameters."""
        sql = "CREATE VIEW dfe_v_test AS SELECT 1 AS val"
        params = parse_view_parameters(sql)
        assert params == []

    def test_uuid_parameter(self):
        """Test UUID parameter type."""
        sql = """
        CREATE VIEW dfe_v_test AS
        SELECT * FROM users WHERE id = {user_id:UUID}
        """
        params = parse_view_parameters(sql)
        assert len(params) == 1
        assert params[0].clickhouse_type == "UUID"
        assert params[0].python_type == "uuid"


# =============================================================================
# Name Conversion Tests
# =============================================================================


class TestViewNameConversion:
    """Tests for view name ↔ label conversion."""

    def test_name_to_label_standard(self):
        assert view_name_to_label("dfe_v_analytics_user_activity") == "analytics/user_activity"

    def test_name_to_label_system(self):
        assert view_name_to_label("dfe_v_system_health") == "system/health"

    def test_name_to_label_deep_name(self):
        """Underscore-separated names after namespace stay as underscores."""
        assert (
            view_name_to_label("dfe_v_hunts_active_threats")
            == "hunts/active_threats"
        )

    def test_name_to_label_invalid_prefix(self):
        """Should raise ValueError if prefix doesn't match."""
        with pytest.raises(ValueError, match="must start with"):
            view_name_to_label("some_other_view")

    def test_name_to_label_single_segment(self):
        """Single segment after prefix — namespace only."""
        assert view_name_to_label("dfe_v_health") == "health"

    def test_label_to_name_standard(self):
        assert label_to_view_name("analytics/user_activity") == "dfe_v_analytics_user_activity"

    def test_label_to_name_system(self):
        assert label_to_view_name("system/health") == "dfe_v_system_health"

    def test_roundtrip(self):
        """Name → label → name should be identity."""
        original = "dfe_v_analytics_event_counts"
        label = view_name_to_label(original)
        restored = label_to_view_name(label)
        assert restored == original


# =============================================================================
# ViewCatalog Tests
# =============================================================================


class TestViewCatalog:
    """Tests for ViewCatalog discovery and caching."""

    def _make_mock_client(self, rows: list[tuple]) -> MagicMock:
        """Create a mock clickhouse-connect client that returns rows."""
        client = MagicMock()
        result = MagicMock()
        result.result_rows = rows
        client.query.return_value = result
        return client

    def test_refresh_discovers_views(self):
        """Test that refresh populates the cache from system.tables."""
        rows = [
            (
                "dfe_v_system_health",
                "CREATE VIEW dfe_v_system_health AS SELECT 1 WHERE org_id = {org_id:String}",
                "2026-01-01 00:00:00",
            ),
            (
                "dfe_v_analytics_events",
                "CREATE VIEW dfe_v_analytics_events AS SELECT * FROM events WHERE org_id = {org_id:String} LIMIT {limit:UInt32}",
                "2026-01-02 00:00:00",
            ),
        ]
        client = self._make_mock_client(rows)
        catalog = ViewCatalog(client=client, database="testdb")
        catalog.refresh()

        views = catalog.list_views()
        assert len(views) == 2
        labels = [v.label for v in views]
        assert "analytics/events" in labels
        assert "system/health" in labels

    def test_get_view(self):
        """Test getting a specific view by label."""
        rows = [
            (
                "dfe_v_system_health",
                "CREATE VIEW dfe_v_system_health AS SELECT 1 WHERE org_id = {org_id:String}",
                None,
            ),
        ]
        client = self._make_mock_client(rows)
        catalog = ViewCatalog(client=client, database="testdb")

        view = catalog.get_view("system/health")
        assert view.label == "system/health"
        assert view.namespace == "system"
        assert view.short_name == "health"

    def test_get_view_not_found(self):
        """Test KeyError when view doesn't exist."""
        client = self._make_mock_client([])
        catalog = ViewCatalog(client=client, database="testdb")

        with pytest.raises(KeyError, match="not found"):
            catalog.get_view("nonexistent/view")

    def test_namespace_filter(self):
        """Test listing views filtered by namespace."""
        rows = [
            (
                "dfe_v_system_health",
                "CREATE VIEW dfe_v_system_health AS SELECT 1",
                None,
            ),
            (
                "dfe_v_analytics_events",
                "CREATE VIEW dfe_v_analytics_events AS SELECT 1",
                None,
            ),
        ]
        client = self._make_mock_client(rows)
        catalog = ViewCatalog(client=client, database="testdb")

        system_views = catalog.list_views(namespace="system")
        assert len(system_views) == 1
        assert system_views[0].namespace == "system"

    def test_get_namespaces(self):
        """Test getting unique namespace list."""
        rows = [
            ("dfe_v_system_health", "CREATE VIEW ...", None),
            ("dfe_v_system_table_sizes", "CREATE VIEW ...", None),
            ("dfe_v_analytics_events", "CREATE VIEW ...", None),
        ]
        client = self._make_mock_client(rows)
        catalog = ViewCatalog(client=client, database="testdb")

        namespaces = catalog.get_namespaces()
        assert namespaces == ["analytics", "system"]

    def test_cache_ttl(self):
        """Test that cache is used within TTL."""
        rows = [
            ("dfe_v_system_health", "CREATE VIEW ...", None),
        ]
        client = self._make_mock_client(rows)
        catalog = ViewCatalog(client=client, database="testdb", cache_ttl=60)

        # First call triggers refresh
        catalog.list_views()
        assert client.query.call_count == 1

        # Second call uses cache
        catalog.list_views()
        assert client.query.call_count == 1  # Not incremented

    def test_tenant_isolation_detected(self):
        """Test that tenant isolation is detected from org_id parameter."""
        rows = [
            (
                "dfe_v_analytics_events",
                "CREATE VIEW dfe_v_analytics_events AS SELECT * FROM events WHERE org_id = {org_id:String}",
                None,
            ),
            (
                "dfe_v_system_global",
                "CREATE VIEW dfe_v_system_global AS SELECT version()",
                None,
            ),
        ]
        client = self._make_mock_client(rows)
        catalog = ViewCatalog(client=client, database="testdb")

        events = catalog.get_view("analytics/events")
        assert events.tenant_isolated is True

        global_view = catalog.get_view("system/global")
        assert global_view.tenant_isolated is False

    def test_parameters_extracted(self):
        """Test that parameters are correctly extracted from create SQL."""
        sql = (
            "CREATE VIEW dfe_v_analytics_events AS "
            "SELECT * FROM events "
            "WHERE org_id = {org_id:String} "
            "AND timestamp >= {time_from:DateTime64(3)} "
            "LIMIT {limit:UInt32}"
        )
        rows = [("dfe_v_analytics_events", sql, None)]
        client = self._make_mock_client(rows)
        catalog = ViewCatalog(client=client, database="testdb")

        view = catalog.get_view("analytics/events")
        assert len(view.parameters) == 3

        names = [p.name for p in view.parameters]
        assert names == ["org_id", "time_from", "limit"]

        # Check org_id is marked reserved
        org_param = view.parameters[0]
        assert org_param.reserved is True
