"""
ClickHouse datasource adapter.
"""

from __future__ import annotations

import re
from typing import Any

import pyarrow as pa

from dfe_engine.query.datasources import DatasourceAdapter, register_adapter
from dfe_engine.query.models import ExplainPlan, ExplainStep, ExplainStepType


@register_adapter("clickhouse")
class ClickHouseAdapter(DatasourceAdapter):
    """
    ClickHouse datasource adapter.

    Uses clickhouse-connect for native Arrow support.
    """

    def __init__(self, target: str, config: dict[str, Any] | None = None):
        super().__init__(target, config)
        self._manager = None
        self._restricted_client = None

    @property
    def manager(self):
        """Lazy-load ClickHouseManager."""
        if self._manager is None:
            from dfe_engine.clickhouse import ClickHouseManager

            # Get manager by target name or use default
            if self.config:
                self._manager = ClickHouseManager.get_instance(self.config)
            else:
                self._manager = ClickHouseManager.get_instance_by_target(self.target)
        return self._manager

    def get_restricted_client(self) -> Any:
        """Get a restricted clickhouse-connect client for parameterized view execution.

        Creates a separate connection authenticated as the restricted query user.
        This client can ONLY SELECT from dfe_v_* views (enforced by ClickHouse RBAC).

        Returns:
            clickhouse-connect Client instance
        """
        if self._restricted_client is None:
            import clickhouse_connect

            from dfe_engine.settings import get_settings

            settings = get_settings()
            ch = settings.clickhouse
            qv = settings.query_views

            connect_params: dict[str, Any] = {
                "host": ch.host,
                "port": ch.port,
                "username": qv.restricted_user,
                "password": qv.restricted_password,
                "database": ch.database,
            }

            if ch.secure:
                connect_params["secure"] = True
                connect_params["verify"] = ch.verify

            self._restricted_client = clickhouse_connect.get_client(**connect_params)

        return self._restricted_client

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
    ) -> pa.Table:
        """
        Execute query and return Arrow Table.

        clickhouse-connect natively returns Arrow format.
        """
        client = self.manager.get_clickhouse_client()

        # clickhouse-connect query_arrow returns PyArrow Table
        settings = {"max_execution_time": timeout_seconds}

        result = client.query_arrow(
            query,
            parameters=params or {},
            settings=settings,
        )

        return result

    def explain(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> ExplainPlan:
        """
        Get EXPLAIN plan from ClickHouse.

        Uses EXPLAIN PLAN or EXPLAIN ESTIMATE.
        """
        client = self.manager.get_clickhouse_client()

        # Get both plan types for comprehensive info
        explain_query = f"EXPLAIN PLAN {query}"

        try:
            result = client.query(explain_query, parameters=params or {})
            raw_plan = "\n".join(str(row[0]) for row in result.result_rows)
        except Exception:
            # Fallback to simpler EXPLAIN
            explain_query = f"EXPLAIN {query}"
            result = client.query(explain_query, parameters=params or {})
            raw_plan = "\n".join(str(row[0]) for row in result.result_rows)

        # Parse the plan into steps
        steps = self._parse_explain_plan(raw_plan)

        # Try to get row estimates
        try:
            estimate_result = client.query(
                f"EXPLAIN ESTIMATE {query}",
                parameters=params or {},
            )
            total_rows = sum(int(row[2]) for row in estimate_result.result_rows if len(row) > 2)
        except Exception:
            total_rows = None

        return ExplainPlan(
            steps=steps,
            total_estimated_rows=total_rows,
            raw_plan=raw_plan,
        )

    def _parse_explain_plan(self, raw_plan: str) -> list[ExplainStep]:
        """Parse ClickHouse EXPLAIN output into steps."""
        steps = []

        for line in raw_plan.split("\n"):
            line = line.strip()
            if not line:
                continue

            step_type = self._classify_step(line)
            steps.append(
                ExplainStep(
                    step_type=step_type,
                    description=line,
                    details=self._extract_details(line),
                )
            )

        return steps

    def _classify_step(self, line: str) -> ExplainStepType:
        """Classify EXPLAIN line into step type."""
        line_lower = line.lower()

        if any(kw in line_lower for kw in ["readfrom", "mergetree", "read"]):
            return ExplainStepType.READ
        if any(kw in line_lower for kw in ["filter", "where", "prewhere"]):
            return ExplainStepType.FILTER
        if any(kw in line_lower for kw in ["aggregat", "group"]):
            return ExplainStepType.AGGREGATE
        if any(kw in line_lower for kw in ["sort", "order"]):
            return ExplainStepType.SORT
        if any(kw in line_lower for kw in ["join"]):
            return ExplainStepType.JOIN
        if any(kw in line_lower for kw in ["expression", "project"]):
            return ExplainStepType.PROJECTION
        if any(kw in line_lower for kw in ["limit"]):
            return ExplainStepType.LIMIT
        if any(kw in line_lower for kw in ["union"]):
            return ExplainStepType.UNION

        return ExplainStepType.UNKNOWN

    def _extract_details(self, line: str) -> dict[str, Any] | None:
        """Extract structured details from EXPLAIN line."""
        details = {}

        # Extract table name
        table_match = re.search(r"ReadFrom(?:MergeTree)?\s*\(([^)]+)\)", line)
        if table_match:
            details["table"] = table_match.group(1)

        # Extract row estimates
        rows_match = re.search(r"(\d+)\s*rows", line, re.IGNORECASE)
        if rows_match:
            details["estimated_rows"] = int(rows_match.group(1))

        return details if details else None

    def healthcheck(self) -> bool:
        """Check ClickHouse connectivity."""
        try:
            client = self.manager.get_clickhouse_client()
            result = client.query("SELECT 1")
            return result.result_rows[0][0] == 1
        except Exception:
            return False

    def close(self) -> None:
        """Close ClickHouse connections."""
        if self._restricted_client:
            self._restricted_client.close()
            self._restricted_client = None
        if self._manager:
            self._manager.close()
            self._manager = None
