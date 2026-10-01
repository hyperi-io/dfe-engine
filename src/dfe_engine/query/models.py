"""
Pydantic models for Query API requests and metadata.

This module defines the data structures for the secure Query API where:
- Clients reference queries by label (not raw SQL)
- ClickHouse parameterized views are the primary execution path
- Parameters are validated against view definitions
- Multi-tenant isolation is enforced via org_id from JWT
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# =============================================================================
# Exceptions -- re-exported from auth.models for backward compat
# =============================================================================
from dfe_engine.auth.models import AuthorizationError as AuthorizationError

# =============================================================================
# Query Request Models (Client -> Server)
# =============================================================================


class QueryRequest(BaseModel):
    """
    Request payload for query execution.

    Clients specify a query label and parameters - never raw SQL.
    The SQL is resolved server-side via parameterized views.
    """

    query: str = Field(
        ...,
        description="Query label (e.g., 'analytics/user_activity', 'hunts/active_threats')",
        examples=["analytics/user_activity", "hunts/active_threats", "system/health"],
        pattern=r"^[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$",
    )
    params: dict[str, Any] | None = Field(
        default=None,
        description="Query parameters (validated against view definition)",
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
    - Keyset: Use `order_by` with `after_key` (and `tiebreak_by` with
      `after_tiebreak` when the key is not unique) for stable pagination

    A view that declares its own `limit` parameter caps its rows before any outer
    paging applies, so `order_by`, `after_key` and `offset` are refused on it.
    """

    # Pagination - offset-based
    limit: int | None = Field(default=None, ge=1, le=100_000)
    offset: int | None = Field(default=None, ge=0, description="Not combinable with after_key")

    # Pagination - keyset-based
    after_key: Any | None = Field(
        default=None,
        description=(
            "order_by value of the previous page's last row (a string or a number); "
            "requires order_by. A timestamp is sent as e.g. '2024-01-15T12:00:00'"
        ),
    )
    order_by: str | None = Field(
        default=None,
        description=(
            "Column to order and page by. Rows whose key is NULL are never returned by "
            "keyset paging, and without tiebreak_by the key must be unique: rows tying "
            "with the cursor are skipped"
        ),
    )
    order_dir: Literal["asc", "desc"] = Field(
        default="asc",
        description="Sort direction for ordering",
    )
    tiebreak_by: str | None = Field(
        default=None,
        description="Second, unique, non-NULL column that orders rows tying on order_by",
    )
    after_tiebreak: Any | None = Field(
        default=None,
        description="tiebreak_by value of the previous page's last row; sent with after_key",
    )

    # Time bounds (for time-series queries)
    time_from: str | None = Field(default=None, description="ISO8601 datetime")
    time_to: str | None = Field(default=None, description="ISO8601 datetime")

    # Execution
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)

    # Caching
    cache: bool = Field(default=True, description="Allow cached results")

    # Store override (only if query definition allows store: '*')
    store: str | None = Field(
        default=None,
        description="Target store (database/schema/topic) - only if query allows",
    )


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

    def to_dict(self) -> dict[str, Any]:
        """Serialise to dict for JSON responses."""
        return self.model_dump(mode="json")


# =============================================================================
# Auth Context Models
# =============================================================================


# AuthContext moved to auth.models -- re-export for backward compat
from dfe_engine.auth.models import AuthContext as AuthContext

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


# =============================================================================
# Parameterized View Models
# =============================================================================


class ViewParameter(BaseModel):
    """Parameter discovered from a ClickHouse parameterized view.

    Extracted from the view's CREATE SQL by parsing {param:Type} patterns.
    Provides type metadata across the full stack (ClickHouse -> Python -> TypeScript -> HTML).
    """

    name: str = Field(..., description="Parameter name as declared in the view")
    clickhouse_type: str = Field(
        ...,
        description="ClickHouse type (String, UInt64, DateTime64(3), Array(UInt64))",
    )
    python_type: str = Field(
        ...,
        description="Python type mapping (string, integer, float, datetime, date, uuid, boolean, array)",
    )
    typescript_type: str = Field(
        default="string",
        description="TypeScript type mapping (string, number, boolean, string[], number[])",
    )
    input_type: str = Field(
        default="text",
        description="HTML input type hint (text, number, datetime-local, date, checkbox, select, multiselect)",
    )
    required: bool = Field(
        default=True,
        description="Whether the parameter is required (all CH view params are required)",
    )
    reserved: bool = Field(
        default=False,
        description="If true, parameter is injected server-side (e.g. org_id) and hidden from UI",
    )
    description: str | None = Field(default=None, description="Human-readable description")
    placeholder: str | None = Field(default=None, description="Example value for UI input fields")
    enum: list[Any] | None = Field(
        default=None,
        description="Allowed values (renders as dropdown in UI)",
    )


class ViewDefinition(BaseModel):
    """A parameterized view discovered from ClickHouse system.tables.

    Views follow the naming convention: dfe_v_{namespace}_{name}
    e.g. dfe_v_analytics_user_activity -> label: analytics/user_activity
    """

    name: str = Field(..., description="Full view name (e.g. dfe_v_analytics_user_activity)")
    label: str = Field(
        ...,
        description="Derived label for API consumers (e.g. analytics/user_activity)",
    )
    database: str = Field(..., description="ClickHouse database containing the view")
    parameters: list[ViewParameter] = Field(default_factory=list)
    create_sql: str = Field(..., description="CREATE VIEW statement from system.tables")
    modified_at: str | None = Field(
        default=None,
        description="Last modification timestamp from system.tables",
    )
    namespace: str = Field(..., description="Namespace derived from view name (e.g. analytics)")
    short_name: str = Field(
        ...,
        description="Short name within namespace (e.g. user_activity)",
    )
    description: str | None = Field(default=None, description="View description")
    tenant_isolated: bool = Field(
        default=True,
        description="Whether view contains org_id parameter for tenant isolation",
    )
    required_roles: list[str] = Field(default_factory=list)


class ViewExecuteRequest(BaseModel):
    """Request to execute a parameterized view."""

    params: dict[str, Any] = Field(
        default_factory=dict,
        description="View parameters (validated against view definition)",
    )
    options: QueryOptions | None = Field(
        default=None,
        description="Execution options (limit, offset, timeout, etc.)",
    )
