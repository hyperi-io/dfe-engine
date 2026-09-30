#  Project:      dfe-engine
#  File:         src/dfe_engine/query/datasources/clickhouse.py
#  Purpose:      ClickHouse datasource adapter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""ClickHouse datasource adapter using clickhouse-connect."""

from __future__ import annotations

import re
from typing import Any

from clickhouse_connect.driver.exceptions import DatabaseError

from dfe_engine.query.datasources import DatasourceAdapter, register_adapter
from dfe_engine.query.models import ExplainPlan, ExplainStep, ExplainStepType


def _estimated_rows(estimate: Any) -> int | None:
    """Rows EXPLAIN ESTIMATE expects the query to read; None when it lists no MergeTree read."""
    if not estimate.result_rows:
        return None
    rows_at = list(estimate.column_names).index("rows")
    return sum(int(row[rows_at]) for row in estimate.result_rows)


@register_adapter("clickhouse")
class ClickHouseAdapter(DatasourceAdapter):
    """ClickHouse datasource adapter using clickhouse-connect."""

    def __init__(self, target: str, config: dict[str, Any] | None = None):
        super().__init__(target, config)
        self._manager = None
        self._restricted_client = None

    @property
    def manager(self):
        """This adapter's ClickHouseManager, built on first use.

        A config gets a manager of its own, which ``close()`` cleans up. With no config
        the adapter uses the process-wide manager the engine seeds at startup, and never
        closes it.
        """
        if self._manager is None:
            from dfe_engine.clickhouse import ClickHouseManager

            if self.config:
                self._manager = ClickHouseManager(self.config)
            else:
                self._manager = ClickHouseManager.get_instance()
        return self._manager

    def get_restricted_client(self) -> Any:
        """Get a restricted clickhouse-connect client for parameterized view execution."""
        if self._restricted_client is None:
            import clickhouse_connect

            from dfe_engine.clickhouse.tls import resolve_clickhouse_tls
            from dfe_engine.settings import get_settings

            settings = get_settings()
            ch = settings.clickhouse
            qv = settings.query_views
            tls = resolve_clickhouse_tls(secure=ch.secure, verify=ch.verify, ca_cert=ch.ca_cert)

            connect_params: dict[str, Any] = {
                "host": ch.host,
                "port": ch.port,
                "username": qv.restricted_user,
                "password": qv.restricted_password,
                "database": ch.database,
            }
            connect_params.update(tls.connect_kwargs())

            self._restricted_client = clickhouse_connect.get_client(**connect_params)

        return self._restricted_client

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: int = 30,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Execute a read-only query and return (rows, column_names).

        The query runs as the engine's own ClickHouse user, which may write, so it
        is sent with ``readonly=1``. ClickHouse then refuses every write and DDL
        statement, any ``INSERT INTO FUNCTION`` (``url()``, ``file()``, ``s3()``,
        ...), any read through ``url()``, and any ``SETTINGS`` clause in the query.
        ``readonly=2`` would allow those table-function writes and reads. The
        ``max_execution_time`` sent with the query still applies.
        """
        client = self.manager.get_clickhouse_client()

        settings = {"max_execution_time": timeout_seconds, "readonly": 1}
        result = client.query(query, parameters=params or {}, settings=settings)

        columns = list(result.column_names)
        rows = [dict(zip(columns, row, strict=True)) for row in result.result_rows]
        if not columns:
            columns = self._describe_columns(client, query, params, settings)
        return rows, columns

    def _describe_columns(
        self, client: Any, query: str, params: dict[str, Any] | None, settings: dict[str, Any]
    ) -> list[str]:
        """Column names of a query that returned no rows, from ClickHouse's analysis of it.

        ClickHouse sends no Native block for an empty result, so the names are not in the
        response. DESCRIBE analyses the query without running it; a statement it cannot
        wrap, such as SHOW or one with a FORMAT clause, has no names to give.
        """
        # The newline ends a trailing line comment before the closing parenthesis.
        described_sql = f"DESCRIBE TABLE (\n{query.rstrip().rstrip(';')}\n)"
        try:
            described = client.query(described_sql, parameters=params or {}, settings=settings)
        except DatabaseError:
            return []
        return [str(row[0]) for row in described.result_rows]

    def explain(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> ExplainPlan:
        """Get EXPLAIN plan from ClickHouse."""
        client = self.manager.get_clickhouse_client()

        explain_query = f"EXPLAIN PLAN {query}"
        try:
            result = client.query(explain_query, parameters=params or {})
            raw_plan = "\n".join(str(row[0]) for row in result.result_rows)
        except Exception:
            explain_query = f"EXPLAIN {query}"
            result = client.query(explain_query, parameters=params or {})
            raw_plan = "\n".join(str(row[0]) for row in result.result_rows)

        steps = self._parse_explain_plan(raw_plan)

        try:
            estimate = client.query(f"EXPLAIN ESTIMATE {query}", parameters=params or {})
            total_rows = _estimated_rows(estimate)
        except Exception:
            total_rows = None

        return ExplainPlan(
            steps=steps,
            total_estimated_rows=total_rows,
            raw_plan=raw_plan,
        )

    def _parse_explain_plan(self, raw_plan: str) -> list[ExplainStep]:
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
        details = {}
        table_match = re.search(r"ReadFrom(?:MergeTree)?\s*\(([^)]+)\)", line)
        if table_match:
            details["table"] = table_match.group(1)
        rows_match = re.search(r"(\d+)\s*rows", line, re.IGNORECASE)
        if rows_match:
            details["estimated_rows"] = int(rows_match.group(1))
        return details if details else None

    def healthcheck(self) -> bool:
        try:
            client = self.manager.get_clickhouse_client()
            result = client.query("SELECT 1")
            return result.result_rows[0][0] == 1
        except Exception:
            return False

    def close(self) -> None:
        """Close what this adapter opened: its restricted client, and a manager it built."""
        if self._restricted_client:
            self._restricted_client.close()
            self._restricted_client = None
        if self._manager is not None and self.config:
            self._manager.close()
        self._manager = None
