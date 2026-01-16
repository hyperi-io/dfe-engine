"""
DFE Query API - Unified query interface with Arrow-native output.

This module provides a standardized query interface across multiple datasources
(ClickHouse, PostgreSQL, Prometheus) with Apache Arrow as the wire format.

Example:
    from dfe_engine.query import QueryClient

    client = QueryClient(base_url="http://localhost:8000")

    # Get Arrow Table
    table = client.query("clickhouse:default", "SELECT * FROM logs LIMIT 100")

    # Get pandas DataFrame (zero-copy)
    df = client.query_df("clickhouse:default", "SELECT * FROM logs LIMIT 100")

    # With EXPLAIN plan
    table, explain = client.query_with_explain(
        "clickhouse:default",
        "SELECT * FROM logs WHERE level = 'ERROR'"
    )
"""

from dfe_engine.query.client import QueryClient
from dfe_engine.query.models import (
    QueryRequest,
    QueryMetadata,
    ExplainPlan,
    ExplainStep,
)
from dfe_engine.query.result import QueryResult
from dfe_engine.query.datasources import get_adapter, register_adapter

__all__ = [
    "QueryClient",
    "QueryRequest",
    "QueryMetadata",
    "QueryResult",
    "ExplainPlan",
    "ExplainStep",
    "get_adapter",
    "register_adapter",
]
