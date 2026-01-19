"""Unit tests for QueryRegistry."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dfe_engine.query.models import (
    ParameterDefinition,
    ParameterType,
    QueryDefinition,
)
from dfe_engine.query.registry import (
    QueryNotFoundError,
    QueryRegistry,
    QueryRegistryError,
    QueryValidationError,
)


class TestQueryRegistryInit:
    """Test QueryRegistry initialization."""

    def test_init_default(self):
        """Test default initialization."""
        registry = QueryRegistry()

        assert registry._postgres_dsn is None
        assert registry._fallback_directory is None
        assert len(registry) == 0

    def test_init_with_dsn(self):
        """Test initialization with PostgreSQL DSN."""
        registry = QueryRegistry(postgres_dsn="postgresql://localhost/test")

        assert registry._postgres_dsn == "postgresql://localhost/test"

    def test_init_with_fallback_directory(self):
        """Test initialization with fallback directory."""
        registry = QueryRegistry(fallback_directory="/etc/queries")

        assert registry._fallback_directory == Path("/etc/queries")

    def test_singleton_instance(self):
        """Test singleton pattern."""
        QueryRegistry.reset_instance()

        instance1 = QueryRegistry.get_instance()
        instance2 = QueryRegistry.get_instance()

        assert instance1 is instance2

        QueryRegistry.reset_instance()


class TestQueryRegistryFileLoading:
    """Test loading queries from YAML files."""

    @pytest.fixture
    def query_yaml_file(self, tmp_path):
        """Create a temporary YAML file with queries."""
        yaml_content = """
queries:
  analytics/user_activity:
    datasource: clickhouse:default
    store: events
    sql: |
      SELECT user_id, count() as events
      FROM {{ store }}.logs
      WHERE org_id = {{ _org_id }}
      LIMIT {{ limit }}
    parameters:
      event_type:
        type: string
        required: false
    tenant_isolated: true

  hunts/active_threats:
    datasource: clickhouse:default
    store: security
    sql: |
      SELECT alert_id, severity
      FROM {{ store }}.alerts
      WHERE org_id = {{ _org_id }}
      AND severity IN {{ severities | sql_array }}
      LIMIT {{ limit }}
    parameters:
      severities:
        type: array
        items: string
        required: true
    tenant_isolated: true
    required_roles:
      - analyst
"""
        yaml_file = tmp_path / "queries.yaml"
        yaml_file.write_text(yaml_content)
        return yaml_file

    def test_load_from_file(self, query_yaml_file):
        """Test loading queries from YAML file."""
        registry = QueryRegistry()
        count = registry.load_from_file(query_yaml_file)

        assert count == 2
        assert "analytics/user_activity" in registry
        assert "hunts/active_threats" in registry

    def test_load_from_file_not_found(self):
        """Test error when file not found."""
        registry = QueryRegistry()

        with pytest.raises(FileNotFoundError):
            registry.load_from_file("/nonexistent/queries.yaml")

    def test_load_from_directory(self, tmp_path, query_yaml_file):
        """Test loading from directory."""
        registry = QueryRegistry()
        count = registry.load_from_directory(tmp_path)

        assert count == 2

    def test_load_query_definition(self, query_yaml_file):
        """Test loaded query definition structure."""
        registry = QueryRegistry()
        registry.load_from_file(query_yaml_file)

        query_def = registry.get("analytics/user_activity")

        assert query_def.datasource == "clickhouse:default"
        assert query_def.store == "events"
        assert query_def.tenant_isolated is True
        assert "event_type" in query_def.parameters


class TestQueryRegistryValidation:
    """Test query definition validation."""

    def test_invalid_label_format(self):
        """Test validation of label format."""
        registry = QueryRegistry()

        with pytest.raises(QueryValidationError, match="Invalid query label"):
            registry._register_query(
                "INVALID-Label",
                {
                    "datasource": "clickhouse:default",
                    "store": "events",
                    "sql": "SELECT 1",
                },
            )

    def test_tenant_isolation_missing_org_id(self):
        """Test validation of tenant isolation."""
        registry = QueryRegistry()

        with pytest.raises(QueryValidationError, match="tenant_isolated=true"):
            registry._register_query(
                "test/query",
                {
                    "datasource": "clickhouse:default",
                    "store": "events",
                    "sql": "SELECT * FROM logs",  # Missing {{ _org_id }}
                    "tenant_isolated": True,
                },
            )

    def test_invalid_jinja2_template(self):
        """Test validation of Jinja2 syntax."""
        registry = QueryRegistry()

        with pytest.raises(QueryValidationError, match="invalid Jinja2"):
            registry._register_query(
                "test/query",
                {
                    "datasource": "clickhouse:default",
                    "store": "events",
                    "sql": "SELECT * FROM {{ unclosed",
                    "tenant_isolated": False,
                },
            )

    def test_valid_non_tenant_isolated_query(self):
        """Test non-tenant-isolated query without org_id filter."""
        registry = QueryRegistry()

        # Should not raise
        registry._register_query(
            "admin/system_stats",
            {
                "datasource": "clickhouse:default",
                "store": "system",
                "sql": "SELECT * FROM system.metrics",
                "tenant_isolated": False,
            },
        )

        assert "admin/system_stats" in registry


class TestQueryRegistryLookup:
    """Test query lookup operations."""

    @pytest.fixture
    def populated_registry(self):
        """Create a registry with queries."""
        registry = QueryRegistry()
        registry.register_query(
            "analytics/user_activity",
            QueryDefinition(
                datasource="clickhouse:default",
                store="events",
                sql="SELECT 1 WHERE org_id = {{ _org_id }}",
                tenant_isolated=True,
            ),
        )
        registry.register_query(
            "analytics/sessions",
            QueryDefinition(
                datasource="clickhouse:default",
                store="events",
                sql="SELECT 2 WHERE org_id = {{ _org_id }}",
                tenant_isolated=True,
            ),
        )
        registry.register_query(
            "hunts/threats",
            QueryDefinition(
                datasource="clickhouse:default",
                store="security",
                sql="SELECT 3 WHERE org_id = {{ _org_id }}",
                tenant_isolated=True,
            ),
        )
        return registry

    def test_get_existing_query(self, populated_registry):
        """Test getting existing query."""
        query_def = populated_registry.get("analytics/user_activity")

        assert query_def.datasource == "clickhouse:default"

    def test_get_nonexistent_query(self, populated_registry):
        """Test error on nonexistent query."""
        with pytest.raises(QueryNotFoundError, match="Query not found"):
            populated_registry.get("invalid/query")

    def test_exists(self, populated_registry):
        """Test exists check."""
        assert populated_registry.exists("analytics/user_activity") is True
        assert populated_registry.exists("invalid/query") is False

    def test_list_all_queries(self, populated_registry):
        """Test listing all queries."""
        queries = populated_registry.list_queries()

        assert len(queries) == 3
        assert "analytics/user_activity" in queries
        assert "hunts/threats" in queries

    def test_list_queries_by_namespace(self, populated_registry):
        """Test listing queries filtered by namespace."""
        analytics_queries = populated_registry.list_queries(namespace="analytics")

        assert len(analytics_queries) == 2
        assert all(q.startswith("analytics/") for q in analytics_queries)


class TestQueryRegistrySqlRendering:
    """Test SQL rendering with Jinja2."""

    @pytest.fixture
    def registry_with_query(self):
        """Create registry with a test query."""
        registry = QueryRegistry()
        registry.register_query(
            "test/query",
            QueryDefinition(
                datasource="clickhouse:default",
                store="events",
                sql="""
                    SELECT *
                    FROM {{ store }}.logs
                    WHERE org_id = {{ _org_id | sql_string }}
                    {% if event_types %}AND event_type IN {{ event_types | sql_array }}{% endif %}
                    LIMIT {{ limit }}
                """,
                tenant_isolated=True,
            ),
        )
        return registry

    def test_render_basic(self, registry_with_query):
        """Test basic SQL rendering."""
        query_def = registry_with_query.get("test/query")
        sql = registry_with_query.render_sql(
            query_def,
            {
                "_org_id": "acme-corp",
                "event_types": None,  # Explicitly None for Jinja2 if check
                "limit": 100,
            },
            store="events",
        )

        assert "FROM events.logs" in sql
        assert "'acme-corp'" in sql
        assert "LIMIT 100" in sql

    def test_render_with_array_filter(self, registry_with_query):
        """Test rendering with sql_array filter."""
        query_def = registry_with_query.get("test/query")
        sql = registry_with_query.render_sql(
            query_def,
            {
                "_org_id": "acme-corp",
                "event_types": ["login", "logout"],
                "limit": 100,
            },
            store="events",
        )

        assert "('login', 'logout')" in sql

    def test_sql_string_filter_escaping(self):
        """Test sql_string filter escapes quotes."""
        assert QueryRegistry._filter_sql_string("O'Brien") == "'O''Brien'"
        assert QueryRegistry._filter_sql_string(None) == "NULL"

    def test_sql_array_filter(self):
        """Test sql_array filter formats arrays."""
        assert QueryRegistry._filter_sql_array(["a", "b"]) == "('a', 'b')"
        assert QueryRegistry._filter_sql_array([1, 2, 3]) == "(1, 2, 3)"
        assert QueryRegistry._filter_sql_array([]) == "(NULL)"

    def test_sql_identifier_filter(self):
        """Test sql_identifier filter sanitizes identifiers."""
        assert QueryRegistry._filter_sql_identifier("table_name") == '"table_name"'
        assert QueryRegistry._filter_sql_identifier("bad;table") == '"badtable"'


class TestQueryRegistryStoreResolution:
    """Test store resolution logic."""

    @pytest.fixture
    def registry(self):
        """Create registry with various store specs."""
        registry = QueryRegistry()
        # Literal store
        registry.register_query(
            "test/literal",
            QueryDefinition(
                datasource="clickhouse:default",
                store="events",
                sql="SELECT 1 WHERE org_id = {{ _org_id }}",
                tenant_isolated=True,
            ),
        )
        # Client-specified store
        registry.register_query(
            "test/client_store",
            QueryDefinition(
                datasource="clickhouse:default",
                store="*",
                sql="SELECT 1 WHERE org_id = {{ _org_id }}",
                tenant_isolated=True,
            ),
        )
        return registry

    def test_resolve_literal_store(self, registry):
        """Test resolving literal store name."""
        query_def = registry.get("test/literal")
        stores = registry.resolve_store(query_def, None)

        assert stores == ["events"]

    def test_resolve_client_specified_store(self, registry):
        """Test resolving client-specified store."""
        query_def = registry.get("test/client_store")
        stores = registry.resolve_store(query_def, "analytics_db")

        assert stores == ["analytics_db"]

    def test_client_store_required(self, registry):
        """Test error when client store required but not provided."""
        query_def = registry.get("test/client_store")

        with pytest.raises(QueryValidationError, match="requires client to specify store"):
            registry.resolve_store(query_def, None)

    def test_protected_store_blocked(self, registry):
        """Test protected stores are blocked."""
        query_def = registry.get("test/client_store")

        with pytest.raises(QueryValidationError, match="not permitted"):
            registry.resolve_store(query_def, "system")

    def test_resolve_glob_pattern(self, registry):
        """Test resolving glob pattern."""
        registry.register_query(
            "test/glob",
            QueryDefinition(
                datasource="clickhouse:default",
                store="events_*",
                sql="SELECT 1 WHERE org_id = {{ _org_id }}",
                tenant_isolated=True,
            ),
        )
        query_def = registry.get("test/glob")

        stores = registry.resolve_store(
            query_def,
            None,
            available_stores=["events_2024", "events_2023", "logs"],
        )

        assert stores == ["events_2024", "events_2023"]

    def test_resolve_regex_pattern(self, registry):
        """Test resolving regex pattern."""
        registry.register_query(
            "test/regex",
            QueryDefinition(
                datasource="clickhouse:default",
                store="/events_\\d{4}/",
                sql="SELECT 1 WHERE org_id = {{ _org_id }}",
                tenant_isolated=True,
            ),
        )
        query_def = registry.get("test/regex")

        stores = registry.resolve_store(
            query_def,
            None,
            available_stores=["events_2024", "events_2023", "events_old", "logs"],
        )

        assert stores == ["events_2024", "events_2023"]


class TestQueryRegistryCacheKey:
    """Test cache key generation."""

    def test_generate_cache_key_deterministic(self):
        """Test cache key is deterministic."""
        registry = QueryRegistry()

        key1 = registry.generate_cache_key(
            "test/query",
            {"a": 1, "b": 2},
            "events",
        )
        key2 = registry.generate_cache_key(
            "test/query",
            {"b": 2, "a": 1},  # Different order
            "events",
        )

        assert key1 == key2

    def test_generate_cache_key_unique(self):
        """Test different params produce different keys."""
        registry = QueryRegistry()

        key1 = registry.generate_cache_key("test/query", {"a": 1}, "events")
        key2 = registry.generate_cache_key("test/query", {"a": 2}, "events")

        assert key1 != key2


class TestQueryRegistryPostgresGracefulFallback:
    """Test PostgreSQL graceful fallback behavior."""

    @pytest.fixture
    def fallback_dir(self, tmp_path):
        """Create fallback directory with query files."""
        yaml_content = """
queries:
  fallback/query:
    datasource: clickhouse:default
    store: events
    sql: SELECT 1 WHERE org_id = {{ _org_id }}
    tenant_isolated: true
"""
        (tmp_path / "queries.yaml").write_text(yaml_content)
        return tmp_path

    def test_load_without_dsn_uses_files(self, fallback_dir):
        """Test loading without DSN uses file fallback."""
        registry = QueryRegistry(fallback_directory=fallback_dir)
        count = registry.load()

        assert count == 1
        assert "fallback/query" in registry

    def test_load_from_postgres_without_dsn(self, fallback_dir):
        """Test load_from_postgres without DSN falls back to files."""
        registry = QueryRegistry(fallback_directory=fallback_dir)
        count = registry.load_from_postgres()

        assert count == 1
        assert "fallback/query" in registry

    @patch("dfe_engine.query.registry.QueryRegistry._get_pg_pool")
    def test_load_with_pg_connection_error(self, mock_pool, fallback_dir):
        """Test fallback on PostgreSQL connection error."""
        mock_pool.side_effect = Exception("Connection refused")

        registry = QueryRegistry(
            postgres_dsn="postgresql://localhost/test",
            fallback_directory=fallback_dir,
        )
        count = registry.load()

        assert count == 1
        assert "fallback/query" in registry

    @patch("dfe_engine.query.registry.QueryRegistry._get_pg_pool")
    def test_load_with_table_not_found(self, mock_pool, fallback_dir):
        """Test fallback when table doesn't exist."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (False,)  # Table doesn't exist
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_pool.return_value.connection.return_value.__enter__.return_value = mock_conn

        registry = QueryRegistry(
            postgres_dsn="postgresql://localhost/test",
            fallback_directory=fallback_dir,
        )
        count = registry.load()

        assert count == 1
        assert "fallback/query" in registry

    def test_load_no_sources_raises(self):
        """Test error when no queries can be loaded."""
        registry = QueryRegistry()

        with pytest.raises(QueryRegistryError, match="No queries loaded"):
            registry.load()
