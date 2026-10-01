# DFE Query API - Python SDK

**Version:** 2.0.0
**Last Updated:** 2026-07-13

This document specifies how to consume the DFE Query API from Python applications.

---

## Overview

The Query API provides a **secure, label-based interface** for querying multiple datasources (ClickHouse, PostgreSQL, Prometheus). Key security features:

- **No raw SQL from clients** - Queries are referenced by label, SQL is defined server-side
- **Mandatory tenant isolation** - `_org_id` injected from JWT, cannot be overridden
- **Role-based access control** - Queries can require specific roles/permissions
- **Parameter validation** - All parameters validated against server-side schemas
- **Native JSON responses** - dict rows via clickhouse-connect, no Arrow/pyarrow dependency

---

## Requirements

### Minimum Versions

| Package | Version | Purpose |
|---------|---------|---------|
| `httpx` | ≥0.27.0 | HTTP client (for HTTP mode) |
| `pandas` | ≥2.2.0 | DataFrame conversion (optional) |
| Python | ≥3.12 | Runtime |

### Installation

```bash
# If using dfe-engine directly
pip install dfe-engine>=2.0.0

# If consuming API externally
pip install httpx>=0.27.0
```

---

## Quick Start

### Direct Mode (In-Process)

Use when the Query API is in the same process (e.g. inside the dfe-engine API server):

```python
from dfe_engine.query import QueryClient

# Create client in direct mode
client = QueryClient(direct=True)

# Execute query by label with parameters
result = client.query(
    "analytics/user_activity",
    params={"event_types": ["login", "logout"]},
    limit=100,
)

# Access results
print(f"Rows: {result.num_rows}")
df = result.to_pandas()
df.groupby("event_type").count()
```

### HTTP Mode (External Consumer)

Use when consuming the API over HTTP:

```python
from dfe_engine.query import QueryClient

# Create client with API URL
client = QueryClient(base_url="http://localhost:8000")

# Same API as direct mode - always use query labels, never raw SQL
result = client.query(
    "analytics/user_activity",
    params={"event_types": ["login"]},
)
df = result.to_pandas()
```

---

## Security Model

### Key Principles

1. **Query labels, not SQL** - Clients reference pre-defined queries by label
2. **Server-side SQL** - SQL templates stored in registry, clients cannot modify
3. **Automatic tenant isolation** - `_org_id` extracted from JWT and injected
4. **Parameter validation** - All params validated against schema before execution
5. **Role-based access** - Queries can require specific roles/permissions

### Example Security Flow

```python
# Client sends:
result = client.query(
    "hunts/active_threats",  # Query label
    params={"severities": ["critical", "high"]},  # Validated parameters
    limit=500,
)

# Server-side query definition (not visible to client):
# queries:
#   hunts/active_threats:
#     sql: |
#       SELECT alert_id, severity, timestamp
#       FROM {{ store }}.alerts
#       WHERE org_id = {{ _org_id }}  -- INJECTED, cannot be overridden
#       AND severity IN {{ severities | sql_array }}
#       LIMIT {{ limit }}
#     parameters:
#       severities:
#         type: array
#         items: string
#         required: true
#     tenant_isolated: true
#     required_roles: [analyst]
```

---

## API Reference

### QueryClient

```python
class QueryClient:
    def __init__(
        self,
        base_url: str | None = None,  # API URL for HTTP mode
        direct: bool = False,  # Use in-process execution
        timeout_seconds: int = 30,  # Default query timeout
    ): ...
```

### Methods

#### `query(query_label, params?, *, limit?, offset?, order_by?, order_dir?, after_key?, tiebreak_by?, after_tiebreak?, time_from?, time_to?, timeout_seconds?, store?, cache?) -> QueryResult`

Execute a labeled query and return result.

```python
# Simple query
result = client.query("analytics/user_activity")

# With parameters
result = client.query(
    "analytics/user_activity",
    params={"event_types": ["login", "purchase"]},
)

# With standard options
result = client.query(
    "hunts/active_threats",
    params={"severities": ["critical"]},
    limit=500,
    time_from="2024-01-01T00:00:00Z",
    time_to="2024-01-31T23:59:59Z",
    timeout_seconds=120,
)

# With store override (only if query definition allows store='*')
result = client.query(
    "admin/table_stats",
    store="analytics_db",  # Target store
)
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `query_label` | `str` | Query identifier (e.g., `"analytics/user_activity"`) |
| `params` | `dict` | Query parameters (validated against schema) |
| `limit` | `int` | Maximum rows (clamped to query/global max) |
| `offset` | `int` | Skip first N rows (offset-based pagination); not combinable with `after_key` |
| `order_by` | `str` | Column to order and page by (keyset pagination) |
| `order_dir` | `str` | Sort direction: `"asc"` or `"desc"` (default: `"asc"`) |
| `after_key` | `str \| int \| float` | `order_by` value of the previous page's last row |
| `tiebreak_by` | `str` | Unique second column for an `order_by` key that is not unique |
| `after_tiebreak` | `str \| int \| float` | `tiebreak_by` value of the previous page's last row |
| `time_from` | `str` | Start time (ISO8601) for time-bounded queries |
| `time_to` | `str` | End time (ISO8601), defaults to now |
| `timeout_seconds` | `int` | Query timeout (clamped to query/global max) |
| `store` | `str` | Target store (only if query allows) |
| `cache` | `bool` | Allow cached results (default: `True`) |

To get a pandas DataFrame directly, call `.to_pandas()` on the `QueryResult`:

```python
result = client.query(
    "analytics/user_activity",
    params={"event_types": ["login"]},
)
df = result.to_pandas()

# DataFrame operations
df.groupby("event_type").agg({"count": "sum"})
```

To process a large result set without loading everything into memory at
once, page through it with `limit`/`offset` (or keyset options - see
[Pagination](#pagination)) and process each page as it arrives:

```python
# Process a large result set one page at a time
offset = 0
while True:
    result = client.query(
        "analytics/all_events",
        params={"date": "2024-01-15"},
        limit=10_000,
        offset=offset,
    )
    process(result.to_pandas())
    if not result.metadata.has_more:
        break
    offset = result.metadata.next_offset
```

---

## QueryResult

Returned by `query()`.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `rows` | `list[dict[str, Any]]` | Result rows (native clickhouse-connect dicts) |
| `columns` | `list[str]` | Column names |
| `num_rows` | `int` | Number of rows |
| `num_columns` | `int` | Number of columns |
| `metadata` | `QueryMetadata` | Execution metadata |
| `explain` | `ExplainPlan \| None` | Always `None` from `query()`; the client requests no EXPLAIN |

### QueryMetadata

| Field | Type | Description |
|-------|------|-------------|
| `row_count` | `int` | Number of rows returned |
| `query_duration_ms` | `int` | Query execution time |
| `query_label` | `str` | Executed query label |
| `datasource` | `str` | Datasource used |
| `store` | `str` | Store/database used |
| `truncated` | `bool` | Results truncated by limit |
| `cached` | `bool` | Result served from cache |
| `request_id` | `str` | Request tracking ID |
| `has_more` | `bool` | More pages available |
| `next_offset` | `int \| None` | Offset for next page (offset-based) |
| `total_count` | `int \| None` | Total rows (if available) |

### Export Methods

```python
result = client.query("analytics/user_activity")

# Pandas
df = result.to_pandas()

# Python native
rows = result.to_pylist()  # List of dicts
cols = result.to_pydict()  # Dict of lists

# Text formats
json_str = result.to_json()
csv_str = result.to_csv()

# Iteration
for row in result.iter_rows():
    process(row)

# QueryResult itself is iterable/sized
print(len(result))
for row in result:
    process(row)
```

---

## ExplainPlan

Query execution plan from EXPLAIN.

### Structure

```python
class ExplainPlan(BaseModel):
    steps: list[ExplainStep]  # Execution steps
    total_estimated_cost: float | None  # Estimated query cost
    total_estimated_rows: int | None  # Estimated row count
    warnings: list[str]  # Performance warnings
    raw_plan: str | None  # Original EXPLAIN output


class ExplainStep(BaseModel):
    step_type: ExplainStepType  # READ, FILTER, AGGREGATE, etc.
    description: str  # Step description
    estimated_rows: int | None  # Estimated rows for this step
    estimated_cost: float | None  # Estimated cost
    actual_rows: int | None  # Actual rows (if available)
    actual_time_ms: float | None  # Actual time (if available)
    details: dict | None  # Step-specific details
```

### Step Types

| Type | Description |
|------|-------------|
| `READ` | Table/index scan |
| `FILTER` | WHERE/PREWHERE clause |
| `AGGREGATE` | GROUP BY operation |
| `SORT` | ORDER BY operation |
| `JOIN` | Join operation |
| `PROJECTION` | Column selection |
| `LIMIT` | LIMIT clause |
| `UNION` | Union operation |
| `UNKNOWN` | Unclassified step |

---

## Pagination

The Query API supports three pagination modes for different use cases.

### Offset-Based Pagination

Traditional pagination with `limit` and `offset`. Simple but inefficient for deep pages.

```python
# First page
result = client.query("analytics/user_activity", limit=100)

# Second page
result = client.query("analytics/user_activity", limit=100, offset=100)

# Third page
result = client.query("analytics/user_activity", limit=100, offset=200)

# Check for more pages
if result.metadata.has_more:
    next_offset = result.metadata.next_offset
    result = client.query("analytics/user_activity", limit=100, offset=next_offset)
```

### Keyset-Based Pagination

Pagination on a sort key, stable under concurrent inserts. Best for time-series.

```python
# First page (sorted by timestamp descending, event_id breaking ties)
result = client.query(
    "analytics/user_activity",
    limit=100,
    order_by="timestamp",
    order_dir="desc",
    tiebreak_by="event_id",
)

# Next page: the last row's values, sent as a string or a number
while result.metadata.has_more:
    last = result.rows[-1]
    result = client.query(
        "analytics/user_activity",
        limit=100,
        order_by="timestamp",
        order_dir="desc",
        tiebreak_by="event_id",
        after_key=last["timestamp"].strftime("%Y-%m-%dT%H:%M:%S.%f"),
        after_tiebreak=last["event_id"],
    )
```

`after_key` is compared as the `order_by` column's type, so a timestamp goes as `"2024-01-15T12:00:00"` (with a fraction if the column has one), never a `datetime` or pandas `Timestamp` object. The key must be non-NULL, and without `tiebreak_by` it must be unique, or rows tying with the cursor are skipped. A view that declares its own `limit` parameter refuses keyset and offset paging.

### Pagination Recommendations

| Use Case | Mode | Reason |
|----------|------|--------|
| Time-series data | Keyset | Stable under inserts, fast on indexed columns |
| Admin UIs | Offset | Simple, allows jumping to pages |
| Export/ETL | Keyset | Constant cost per page at any depth |

---

## Storage Listing

Query API provides built-in queries for listing files in S3, MinIO, and local filesystems.

### S3 Bucket Listing

```python
# List S3 bucket contents
result = client.query(
    "storage/s3_list",
    params={
        "bucket": "my-bucket",
        "prefix": "logs/2024/",
    },
    limit=1000,
)

df = result.to_pandas()
# Columns: name, path, type, size, modified, etag, storage_class, content_type
print(df[["name", "type", "size"]])
```

### MinIO Listing

```python
# List MinIO bucket (same API as S3)
result = client.query(
    "storage/minio_list",
    params={
        "bucket": "my-bucket",
        "prefix": "data/",
    },
)
```

### Filesystem Listing

```python
# List local directory
result = client.query(
    "storage/file_list",
    params={
        "path": "reports/2024",
        "recursive": True,
        "pattern": "*.json",
    },
)

df = result.to_pandas()
for _, row in df.iterrows():
    print(f"{row['type']:10} {row['size']:>10} {row['name']}")
```

### Storage Listing Schema

All storage adapters return a consistent JSON schema:

| Column | Type | Description |
|--------|------|-------------|
| `name` | `string` | File or directory name |
| `path` | `string` | Full path within storage |
| `type` | `string` | `"file"` or `"directory"` |
| `size` | `integer` | Size in bytes (0 for directories) |
| `modified` | `datetime \| None` | Last modified time, `None` for directories |
| `etag` | `string \| None` | Object ETag (S3/MinIO) |
| `storage_class` | `string \| None` | Storage class (S3/MinIO) |
| `content_type` | `string \| None` | MIME type |

### Storage Pagination

Storage listings page with `limit` and `offset`:

```python
# Page through large bucket
result = client.query(
    "storage/s3_list",
    params={"bucket": "my-bucket", "prefix": "logs/"},
    limit=1000,
)

all_files = []
while True:
    all_files.extend(result.to_pylist())
    if not result.metadata.has_more:
        break
    result = client.query(
        "storage/s3_list",
        params={"bucket": "my-bucket", "prefix": "logs/"},
        offset=result.metadata.next_offset,
        limit=1000,
    )

print(f"Total files: {len(all_files)}")
```

---

## Error Handling

```python
from dfe_engine.query import QueryClient
from dfe_engine.query.registry import QueryNotFoundError
from dfe_engine.query.validator import (
    ParameterValidationError,
    AuthorizationError,
)
import httpx

client = QueryClient(base_url="http://localhost:8000")

try:
    result = client.query(
        "hunts/active_threats",
        params={"severities": ["critical"]},
    )
except QueryNotFoundError as e:
    print(f"Query not found: {e}")
except ParameterValidationError as e:
    print(f"Invalid parameter '{e.param}': {e}")
except AuthorizationError as e:
    print(f"Not authorized: {e}")
except httpx.HTTPStatusError as e:
    if e.response.status_code == 504:
        print("Query timed out")
    else:
        raise
except httpx.ConnectError:
    print("Cannot connect to Query API")
```

### Common Errors

| Error | Cause | Resolution |
|-------|-------|------------|
| `QueryNotFoundError` | Invalid query label | Check query label spelling |
| `ParameterValidationError` | Invalid or missing parameter | Check parameter schema |
| `AuthorizationError` | Missing required role/permission | Contact admin for access |
| `HTTP 504` | Query timeout | Increase timeout or optimize query |

---

## Query Labels

Queries are referenced by labels in `namespace/name` format:

```python
# Analytics namespace
client.query("analytics/user_activity")
client.query("analytics/conversion_funnel")
client.query("analytics/retention_cohorts")

# Hunts namespace
client.query("hunts/active_threats")
client.query("hunts/ioc_matches")
client.query("hunts/anomaly_detection")

# System namespace (requires admin role)
client.query("system/health_check")
client.query("system/table_stats")
```

---

## Performance Tips

### 1. Use Direct Mode When Possible

```python
# HTTP mode adds serialization overhead
client = QueryClient(base_url="http://localhost:8000")

# Direct mode is faster (no HTTP, no extra serialization)
client = QueryClient(direct=True)
```

### 2. Use Time Bounds for Time-Series Data

```python
# GOOD: Limit time range to reduce data scanned
result = client.query(
    "analytics/user_activity",
    time_from="2024-01-01T00:00:00Z",
    time_to="2024-01-02T00:00:00Z",
)

# BAD: No time bounds on time-series table
result = client.query("analytics/user_activity")  # Scans all data
```

### 3. Page Through Large Results

```python
# BAD: Load everything into memory
result = client.query("analytics/all_events", limit=1_000_000)
df = result.to_pandas()  # Memory spike

# GOOD: Page through results and process incrementally
offset = 0
while True:
    result = client.query("analytics/all_events", limit=50_000, offset=offset)
    process_batch(result.to_pandas())
    if not result.metadata.has_more:
        break
    offset = result.metadata.next_offset
```

---

## Integration Examples

### FastAPI Service

```python
from fastapi import FastAPI, Depends, HTTPException
from dfe_engine.query import QueryClient
from dfe_engine.query.validator import AuthorizationError

app = FastAPI()


def get_query_client() -> QueryClient:
    return QueryClient(direct=True)


@app.get("/threats")
async def get_threats(
    severities: list[str] = ["critical", "high"],
    client: QueryClient = Depends(get_query_client),
):
    try:
        result = client.query(
            "hunts/active_threats",
            params={"severities": severities},
            limit=100,
        )
        return {
            "data": result.to_pylist(),
            "metadata": {
                "rows": result.metadata.row_count,
                "duration_ms": result.metadata.query_duration_ms,
                "cached": result.metadata.cached,
            },
        }
    except AuthorizationError:
        raise HTTPException(status_code=403, detail="Not authorized")
```

### CLI Tool

```python
import typer
from rich.console import Console
from rich.table import Table
from dfe_engine.query import QueryClient

app = typer.Typer()
console = Console()


@app.command()
def query(
    label: str,
    limit: int = 50,
):
    """Execute a query by label."""
    client = QueryClient(direct=True)
    result = client.query(label, limit=limit)
    console.print(f"[dim]Duration: {result.metadata.query_duration_ms}ms[/dim]")

    # Display as rich table
    rich_table = Table()
    for col in result.columns:
        rich_table.add_column(col)
    for row in result.to_pylist()[:limit]:
        rich_table.add_row(*[str(v) for v in row.values()])
    console.print(rich_table)
```

---

## References

- [pandas Documentation](https://pandas.pydata.org/docs/)
- [Query Gateway API Specification](query-api.md)
