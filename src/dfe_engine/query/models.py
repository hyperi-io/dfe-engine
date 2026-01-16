"""
Pydantic models for Query API requests and metadata.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """Request payload for query execution."""

    datasource: str = Field(
        ...,
        description="Datasource URI (e.g., 'clickhouse:default', 'postgres:main')",
        examples=["clickhouse:default", "postgres:main", "prometheus:metrics"],
    )
    query: str = Field(..., description="Query string (SQL or PromQL)")
    params: dict[str, Any] | None = Field(
        default=None,
        description="Query parameters for parameterized queries",
    )
    options: QueryOptions | None = Field(
        default=None,
        description="Execution options",
    )


class QueryOptions(BaseModel):
    """Query execution options."""

    limit: int | None = Field(default=None, ge=1, le=1_000_000)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    include_explain: bool = Field(
        default=False,
        description="Include EXPLAIN plan in response headers",
    )
    parallel: bool = Field(
        default=False,
        description="Execute query and EXPLAIN in parallel",
    )


class QueryMetadata(BaseModel):
    """Metadata about query execution (returned in headers)."""

    row_count: int
    query_duration_ms: int
    datasource: str
    truncated: bool = False
    cached: bool = False
    explain_duration_ms: int | None = None


class ExplainStepType(str, Enum):
    """Types of EXPLAIN plan steps."""

    READ = "read"
    FILTER = "filter"
    AGGREGATE = "aggregate"
    SORT = "sort"
    JOIN = "join"
    PROJECTION = "projection"
    LIMIT = "limit"
    UNION = "union"
    UNKNOWN = "unknown"


class ExplainStep(BaseModel):
    """Single step in an EXPLAIN plan."""

    step_type: ExplainStepType
    description: str
    estimated_rows: int | None = None
    estimated_cost: float | None = None
    actual_rows: int | None = None
    actual_time_ms: float | None = None
    details: dict[str, Any] | None = None


class ExplainPlan(BaseModel):
    """Query execution plan from EXPLAIN."""

    steps: list[ExplainStep]
    total_estimated_cost: float | None = None
    total_estimated_rows: int | None = None
    warnings: list[str] = Field(default_factory=list)
    raw_plan: str | None = Field(
        default=None,
        description="Original EXPLAIN output from datasource",
    )

    def to_arrow_metadata(self) -> dict[str, str]:
        """Convert to Arrow schema metadata for embedding in IPC stream."""
        return {
            "dfe:explain:steps": self.model_dump_json(include={"steps"}),
            "dfe:explain:warnings": ",".join(self.warnings),
            "dfe:explain:estimated_cost": str(self.total_estimated_cost or ""),
            "dfe:explain:raw": self.raw_plan or "",
        }

    @classmethod
    def from_arrow_metadata(cls, metadata: dict[bytes, bytes]) -> ExplainPlan | None:
        """Reconstruct ExplainPlan from Arrow schema metadata."""
        import json

        raw_steps = metadata.get(b"dfe:explain:steps")
        if not raw_steps:
            return None

        data = json.loads(raw_steps.decode())
        warnings_str = metadata.get(b"dfe:explain:warnings", b"").decode()

        return cls(
            steps=[ExplainStep(**s) for s in data.get("steps", [])],
            warnings=warnings_str.split(",") if warnings_str else [],
            raw_plan=metadata.get(b"dfe:explain:raw", b"").decode() or None,
            total_estimated_cost=float(
                metadata.get(b"dfe:explain:estimated_cost", b"").decode() or 0
            )
            or None,
        )
