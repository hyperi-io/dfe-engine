"""
Datasource adapters for Query API.

Adapters convert datasource-specific queries to Arrow format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from dfe_engine.query.models import ExplainPlan

if TYPE_CHECKING:
    from concurrent.futures import Future

# Registry of datasource adapters
_adapters: dict[str, type[DatasourceAdapter]] = {}


class DatasourceAdapter(ABC):
    """
    Base class for datasource adapters.

    Subclasses implement query execution and EXPLAIN for specific backends.
    """

    def __init__(self, target: str, config: dict[str, Any] | None = None):
        """
        Initialize adapter.

        Args:
            target: Target identifier (e.g., 'default', 'analytics')
            config: Optional configuration override
        """
        self.target = target
        self.config = config or {}

    @abstractmethod
    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
    ) -> pa.Table:
        """
        Execute query and return Arrow Table.

        Args:
            query: Query string
            params: Optional query parameters
            timeout_seconds: Query timeout

        Returns:
            PyArrow Table with results
        """
        pass

    @abstractmethod
    def explain(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> ExplainPlan:
        """
        Get execution plan for query.

        Args:
            query: Query string
            params: Optional query parameters

        Returns:
            ExplainPlan with execution steps
        """
        pass

    def execute_with_explain(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
        parallel: bool = False,
    ) -> tuple[pa.Table, ExplainPlan]:
        """
        Execute query and return results with EXPLAIN plan.

        Args:
            query: Query string
            params: Optional query parameters
            timeout_seconds: Query timeout
            parallel: Execute query and EXPLAIN concurrently

        Returns:
            Tuple of (Arrow Table, ExplainPlan)
        """
        if parallel:
            return self._execute_parallel(query, params, timeout_seconds)
        else:
            # Sequential execution
            table = self.execute(query, params, timeout_seconds)
            plan = self.explain(query, params)
            return table, plan

    def _execute_parallel(
        self,
        query: str,
        params: dict[str, Any] | None,
        timeout_seconds: int,
    ) -> tuple[pa.Table, ExplainPlan]:
        """Execute query and EXPLAIN in parallel using threads."""
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as executor:
            query_future: Future[pa.Table] = executor.submit(
                self.execute, query, params, timeout_seconds
            )
            explain_future: Future[ExplainPlan] = executor.submit(self.explain, query, params)

            # Wait for both to complete
            table = query_future.result(timeout=timeout_seconds + 5)
            plan = explain_future.result(timeout=timeout_seconds + 5)

        return table, plan

    @abstractmethod
    def healthcheck(self) -> bool:
        """
        Check datasource connectivity.

        Returns:
            True if healthy, False otherwise
        """
        pass

    def close(self) -> None:  # noqa: B027
        """Close any open connections. Override if needed."""
        pass


def register_adapter(name: str):
    """
    Decorator to register a datasource adapter.

    Usage:
        @register_adapter("clickhouse")
        class ClickHouseAdapter(DatasourceAdapter):
            ...
    """

    def decorator(cls: type[DatasourceAdapter]) -> type[DatasourceAdapter]:
        _adapters[name] = cls
        return cls

    return decorator


def get_adapter(datasource: str) -> DatasourceAdapter:
    """
    Get adapter instance for datasource URI.

    Args:
        datasource: URI like 'clickhouse:default' or 'postgres:main'

    Returns:
        Configured DatasourceAdapter instance

    Raises:
        ValueError: Unknown datasource scheme
    """
    scheme, _, target = datasource.partition(":")
    target = target or "default"

    if scheme not in _adapters:
        available = ", ".join(_adapters.keys())
        raise ValueError(f"Unknown datasource scheme: {scheme}. Available: {available}")

    return _adapters[scheme](target)


def list_adapters() -> list[str]:
    """List registered adapter names."""
    return list(_adapters.keys())


# Import adapters to trigger registration
from dfe_engine.query.datasources import clickhouse  # noqa: F401, E402
from dfe_engine.query.datasources import storage  # noqa: F401, E402

# Future: postgres, prometheus
# from dfe_engine.query.datasources import postgres  # noqa: F401, E402
# from dfe_engine.query.datasources import prometheus  # noqa: F401, E402
