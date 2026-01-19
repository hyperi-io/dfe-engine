"""
Pydantic models for Query API requests and metadata.

This module defines the data structures for the secure Query API where:
- Clients reference queries by label (not raw SQL)
- SQL is defined server-side in query registry
- Parameters are validated against schemas
- Multi-tenant isolation is enforced via _org_id from JWT
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# =============================================================================
# Query Request Models (Client -> Server)
# =============================================================================


class QueryRequest(BaseModel):
    """
    Request payload for query execution.

    Clients specify a query label and parameters - never raw SQL.
    The SQL is resolved server-side from the query registry.
    """

    query: str = Field(
        ...,
        description="Query label (e.g., 'analytics/user_activity', 'hunts/active_threats')",
        examples=["analytics/user_activity", "hunts/active_threats", "system/health"],
        pattern=r"^[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$",
    )
    params: dict[str, Any] | None = Field(
        default=None,
        description="Query parameters (validated against query schema)",
    )
    options: QueryOptions | None = Field(
        default=None,
        description="Execution options (limit, timeout, etc.)",
    )


class QueryOptions(BaseModel):
    """
    Query execution options.

    These are standard parameters available for all queries.
    Server enforces maximums from query definition and global config.

    Pagination modes:
    - Offset-based: Use `limit` and `offset` for simple pagination
    - Cursor-based: Use `cursor` for efficient pagination on large datasets
    - Keyset: Use `after_key` with an order column for stable pagination
    """

    # Pagination - offset-based
    limit: int | None = Field(default=None, ge=1, le=100_000)
    offset: int | None = Field(default=None, ge=0)

    # Pagination - cursor-based (mutually exclusive with offset)
    cursor: str | None = Field(
        default=None,
        description="Opaque cursor for cursor-based pagination (from previous response)",
    )

    # Pagination - keyset-based
    after_key: Any | None = Field(
        default=None,
        description="Value to paginate after (requires order_by in query)",
    )
    order_by: str | None = Field(
        default=None,
        description="Column to order by for keyset pagination",
    )
    order_dir: Literal["asc", "desc"] = Field(
        default="asc",
        description="Sort direction for ordering",
    )

    # Time bounds (for time-series queries)
    time_from: str | None = Field(default=None, description="ISO8601 datetime")
    time_to: str | None = Field(default=None, description="ISO8601 datetime")

    # Execution
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)

    # EXPLAIN
    include_explain: bool = Field(default=False)
    explain_parallel: bool = Field(default=True)

    # Caching
    cache: bool = Field(default=True, description="Allow cached results")

    # Store override (only if query definition allows store: '*')
    store: str | None = Field(
        default=None,
        description="Target store (database/schema/topic) - only if query allows",
    )


# =============================================================================
# Query Definition Models (Server-Side Registry)
# =============================================================================


class ParameterType(str, Enum):
    """Supported parameter types for query definitions."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATETIME = "datetime"
    DATE = "date"
    ARRAY = "array"
    UUID = "uuid"


class ParameterDefinition(BaseModel):
    """Definition of a query parameter in the registry."""

    type: ParameterType
    required: bool = False
    default: Any | None = None
    description: str | None = None

    # Type-specific constraints
    min: int | float | None = None
    max: int | float | None = None
    max_length: int | None = None
    pattern: str | None = None  # Regex for strings
    enum: list[Any] | None = None  # Allowed values

    # Array-specific
    items: ParameterType | None = None  # Type of array elements
    max_items: int | None = None

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern: {e}") from e
        return v


class QueryDefinition(BaseModel):
    """
    Server-side query definition loaded from registry.

    SQL is Jinja2 templated with validated parameters.
    """

    # Datasource
    datasource: str = Field(
        ...,
        description="Adapter type (clickhouse, postgres, prometheus)",
        examples=["clickhouse", "postgres"],
    )
    store: str = Field(
        ...,
        description="Database/schema/topic - literal, glob, regex, or '*' for client-specified",
        examples=["events", "logs_*", "/^tenant_\\d+$/", "*"],
    )

    # SQL Template (Jinja2)
    sql: str = Field(..., description="Jinja2 SQL template")

    # Parameters
    parameters: dict[str, ParameterDefinition] = Field(default_factory=dict)

    # Standard parameter defaults/limits
    defaults: QueryDefaults | None = None
    limits: QueryLimits | None = None

    # Time bounding
    time_column: str | None = Field(
        default=None,
        description="Column for time_from/time_to bounds",
    )
    time_required: bool = False
    max_time_range_days: int | None = None

    # Security
    tenant_isolated: bool = Field(
        default=True,
        description="If true, SQL must contain {{ _org_id }}",
    )
    required_roles: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)

    # Caching
    cache_ttl_seconds: int | None = None
    cache_namespace: str | None = None

    # Audit
    audit_level: Literal["none", "basic", "full"] = "full"
    pii_columns: list[str] = Field(default_factory=list)

    # Metadata
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class QueryDefaults(BaseModel):
    """Default values for standard parameters."""

    limit: int | None = None
    timeout_seconds: int | None = None
    cache_ttl: int | None = None


class QueryLimits(BaseModel):
    """Maximum values for standard parameters."""

    max_limit: int | None = None
    max_timeout: int | None = None
    max_time_range_days: int | None = None


# =============================================================================
# Query Response Models
# =============================================================================


class QueryMetadata(BaseModel):
    """Metadata about query execution (returned in headers)."""

    row_count: int
    query_duration_ms: int
    query_label: str
    datasource: str
    store: str | None = None
    truncated: bool = False
    cached: bool = False
    cache_key: str | None = None
    explain_duration_ms: int | None = None
    request_id: str | None = None

    # Pagination info
    has_more: bool = False
    next_cursor: str | None = None
    next_offset: int | None = None
    total_count: int | None = Field(
        default=None,
        description="Total row count (only if query supports it)",
    )


# =============================================================================
# EXPLAIN Plan Models
# =============================================================================


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

        # Handle both list format and dict with "steps" key
        steps_data = data if isinstance(data, list) else data.get("steps", [])

        return cls(
            steps=[ExplainStep(**s) for s in steps_data],
            warnings=warnings_str.split(",") if warnings_str else [],
            raw_plan=metadata.get(b"dfe:explain:raw", b"").decode() or None,
            total_estimated_cost=float(
                metadata.get(b"dfe:explain:estimated_cost", b"").decode() or 0
            )
            or None,
        )


# =============================================================================
# Auth Context Models
# =============================================================================


class AuthContext(BaseModel):
    """
    Authentication context extracted from JWT.

    These values are injected as reserved parameters (_org_id, etc.)
    and cannot be overridden by clients.
    """

    org_id: str = Field(..., description="Tenant organization ID")
    user_id: str = Field(..., description="User ID")
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)

    # Request context (not from JWT)
    request_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None


# =============================================================================
# Audit Models
# =============================================================================


class QueryAuditLog(BaseModel):
    """Audit log entry for query execution."""

    timestamp: str
    request_id: str
    query_label: str
    org_id: str
    user_id: str
    datasource: str
    store: str | None
    params: dict[str, Any] | None = None  # Only if audit_level=full
    duration_ms: int
    row_count: int
    cached: bool
    client_ip: str | None
    user_agent: str | None
    error: str | None = None
