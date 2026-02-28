"""
DFE Query API - Unified query interface with Arrow-native output.

Queries execute via ClickHouse parameterized views discovered from system.tables.
Clients reference views by label (e.g. "analytics/user_activity") — never raw SQL.

Example:
    from dfe_engine.query import QueryClient

    client = QueryClient(base_url="http://localhost:8000")

    # Get Arrow Table
    result = client.query("analytics/user_activity", params={"event_types": ["login"]})

    # Get pandas DataFrame (zero-copy)
    df = client.query_df("analytics/user_activity", params={"event_types": ["login"]})
"""

from dfe_engine.query.catalog import ViewCatalog
from dfe_engine.query.client import QueryClient
from dfe_engine.query.ddl import DDLManager
from dfe_engine.query.executor import ViewExecutionError, ViewExecutor
from dfe_engine.query.models import (
    AuthorizationError,
    ExplainPlan,
    ExplainStep,
    QueryMetadata,
    QueryRequest,
    ViewDefinition,
    ViewExecuteRequest,
    ViewParameter,
)
from dfe_engine.query.result import QueryResult

__all__ = [
    # Client
    "QueryClient",
    # Models
    "QueryRequest",
    "QueryMetadata",
    "QueryResult",
    "ExplainPlan",
    "ExplainStep",
    # Parameterized views
    "ViewCatalog",
    "ViewExecutor",
    "ViewExecutionError",
    "DDLManager",
    "ViewDefinition",
    "ViewParameter",
    "ViewExecuteRequest",
    # Exceptions
    "AuthorizationError",
]
