# DFE Query API - Python client

`dfe_engine.query.QueryClient` ships inside the dfe-engine image. dfe-engine is not published to PyPI, so code outside the image calls the [HTTP API](query-api.md) with its own HTTP client and a bearer token or API key.

---

## Overview

- **No raw SQL from clients** - a view is referenced by label, and its SQL is a ClickHouse parameterised view deployed server-side
- **Mandatory tenant isolation** - `org_id` injected from the org the session's account or API key is bound to, cannot be overridden
- **Scope-based access control** - `query:read` browses the views and `query:execute` runs them
- **Native JSON responses** - dict rows via clickhouse-connect, no Arrow/pyarrow dependency

---

## Requirements

| Package | Purpose |
|---------|---------|
| `pandas` | DataFrame conversion (optional) |
| Python | Runtime |

`pyproject.toml` carries the version floors.

---

## Quick Start

### Direct Mode (In-Process)

Use inside the engine process. Direct mode runs the view through the engine's own `ViewExecutor` with an admin context:

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

### HTTP Mode

HTTP mode posts to `/api/v1/queries/views/{label}/execute` and sends no credentials, so it is answered only by an engine with auth disabled (a dev `DFE_ENGINE_ENV`):

```python
from dfe_engine.query import QueryClient

client = QueryClient(base_url="http://localhost:8000")
result = client.query(
    "analytics/user_activity",
    params={"event_types": ["login"]},
)
df = result.to_pandas()
```

---

## Security Model

A view is a ClickHouse parameterised view deployed through the governed DDL path and discovered by the engine's `ViewCatalog`. There is no YAML query registry and no per-query role list. The executor passes on only the parameters the view declares, unvalidated, and injects `org_id` from the auth context. A missing or mistyped parameter fails in ClickHouse.

```python
result = client.query(
    "hunts/active_threats",                         # view label
    params={"severities": ["critical", "high"]},    # the view's parameters
    limit=500,
)
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
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `query_label` | `str` | Query identifier (e.g., `"analytics/user_activity"`) |
| `params` | `dict` | The view's parameters. Undeclared names are dropped, and values are not validated |
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
| `store` | `str` | Accepted and ignored: the view executor always runs in the data database |
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

The Query API supports two pagination modes, offset and keyset.

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

S3, MinIO and filesystem listings run on the raw route (`POST /api/v1/queries/raw`, scope `raw_query:execute`), which `QueryClient` does not call. The request body and listing columns are in [query-api.md](query-api.md#storage-listing).

---

## Error Handling

Direct mode raises the executor's `ViewExecutionError` (`ViewRequestError` for bad parameters or options) and `KeyError` for an unknown label. HTTP mode raises the HTTP client's status error, carrying the problem response's `code` (see [query-api.md](query-api.md#error-handling)).

```python
from dfe_engine.query import QueryClient
from dfe_engine.query.executor import ViewExecutionError, ViewRequestError

client = QueryClient(direct=True)

try:
    result = client.query("hunts/active_threats", params={"severities": ["critical"]})
except KeyError:
    print("No view with that label")
except ViewRequestError as e:
    print(f"Bad parameters or options: {e}")
except ViewExecutionError as e:
    print(f"ClickHouse failed the view: {e}")
```

---

## Query Labels

A view is referenced by a label in `namespace/name` form. `GET /api/v1/queries/views` lists the labels a deployment has.

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
from dfe_engine.query.executor import ViewRequestError

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
    except ViewRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
