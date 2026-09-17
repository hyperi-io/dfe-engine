"""DFE Query API — unified query interface with JSON output.

Queries execute via ClickHouse parameterized views discovered from system.tables.
Clients reference views by label (e.g. "analytics/user_activity") — never raw SQL.
"""

from dfe_engine.query.catalog import ViewCatalog
from dfe_engine.query.client import QueryClient
from dfe_engine.query.executor import ViewExecutionError, ViewExecutor
from dfe_engine.query.models import (
    AuthorizationError,
    ExplainPlan,
    ExplainStep,
    QueryMetadata,
    ViewDefinition,
    ViewExecuteRequest,
    ViewParameter,
)
from dfe_engine.query.result import QueryResult

__all__ = [
    "QueryClient",
    "QueryMetadata",
    "QueryResult",
    "ExplainPlan",
    "ExplainStep",
    "ViewCatalog",
    "ViewExecutor",
    "ViewExecutionError",
    "ViewDefinition",
    "ViewParameter",
    "ViewExecuteRequest",
    "AuthorizationError",
]
