#  Project:      dfe-engine
#  File:         src/dfe_engine/query/datasources/__init__.py
#  Purpose:      Datasource adapter registry and ABC
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Datasource adapters for Query API.

Adapters execute queries and return rows as list[dict].
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from dfe_engine.query.models import ExplainPlan

# Registry of datasource adapters
_adapters: dict[str, type[DatasourceAdapter]] = {}


class DatasourceAdapter(ABC):
    """Base class for datasource adapters.

    Subclasses implement query execution and EXPLAIN for specific backends.
    """

    def __init__(self, target: str, config: dict[str, Any] | None = None):
        self.target = target
        self.config = config or {}

    @abstractmethod
    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Execute query and return (rows, column_names).

        Returns:
            Tuple of (list of row dicts, list of column name strings)
        """

    @abstractmethod
    def explain(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> ExplainPlan:
        """Get execution plan for query."""

    def execute_with_explain(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
        parallel: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str], ExplainPlan]:
        """Execute query and return results with EXPLAIN plan.

        Returns:
            Tuple of (rows, column_names, ExplainPlan)
        """
        if parallel:
            return self._execute_parallel(query, params, timeout_seconds)
        rows, columns = self.execute(query, params, timeout_seconds)
        plan = self.explain(query, params)
        return rows, columns, plan

    def _execute_parallel(
        self,
        query: str,
        params: dict[str, Any] | None,
        timeout_seconds: int,
    ) -> tuple[list[dict[str, Any]], list[str], ExplainPlan]:
        """Execute query and EXPLAIN in parallel using threads."""
        from concurrent.futures import Future, ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as executor:
            query_future: Future[tuple[list[dict[str, Any]], list[str]]] = executor.submit(
                self.execute, query, params, timeout_seconds
            )
            explain_future: Future[ExplainPlan] = executor.submit(self.explain, query, params)

            rows, columns = query_future.result(timeout=timeout_seconds + 5)
            plan = explain_future.result(timeout=timeout_seconds + 5)

        return rows, columns, plan

    @abstractmethod
    def healthcheck(self) -> bool:
        """Check datasource connectivity."""

    def close(self) -> None:  # noqa: B027
        """Close any open connections. Override if needed."""


def register_adapter(name: str):
    """Decorator to register a datasource adapter."""

    def decorator(cls: type[DatasourceAdapter]) -> type[DatasourceAdapter]:
        _adapters[name] = cls
        return cls

    return decorator


def get_adapter(datasource: str, config: dict[str, Any] | None = None) -> DatasourceAdapter:
    """Get adapter instance for datasource URI (e.g. 'clickhouse:default').

    Args:
        datasource: URI of the form ``scheme:target``.
        config: Optional adapter connection config. For the clickhouse scheme
            pass the settings-derived config (``get_clickhouse_config``) -
            without it the adapter binds the process-wide ClickHouseManager
            singleton to localhost defaults.
    """
    scheme, _, target = datasource.partition(":")
    target = target or "default"

    if scheme not in _adapters:
        available = ", ".join(_adapters.keys())
        raise ValueError(f"Unknown datasource scheme: {scheme}. Available: {available}")

    return _adapters[scheme](target, config)


def list_adapters() -> list[str]:
    """List registered adapter names."""
    return list(_adapters.keys())


# Import adapters to trigger registration
from dfe_engine.query.datasources import (
    clickhouse,  # noqa: F401
    storage,  # noqa: F401
)
