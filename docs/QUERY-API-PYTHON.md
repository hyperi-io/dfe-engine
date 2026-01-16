# DFE Query API - Python SDK

**Version:** 1.0.0
**Last Updated:** 2026-01-16

This document specifies how to consume the DFE Query API from Python applications.

---

## Overview

The Query API provides a unified interface for querying multiple datasources (ClickHouse, PostgreSQL, Prometheus) with Apache Arrow as the wire format. This enables zero-copy data transfer and seamless integration with the Python data ecosystem.

---

## Requirements

### Minimum Versions

| Package | Version | Purpose |
|---------|---------|---------|
| `pyarrow` | ≥22.0.0 | Arrow IPC deserialization |
| `httpx` | ≥0.27.0 | HTTP client (for HTTP mode) |
| `pandas` | ≥2.2.0 | DataFrame conversion (optional) |
| Python | ≥3.12 | Runtime |

### Installation

```bash
# If using dfe-engine directly
pip install dfe-engine>=1.2.0

# If consuming API externally
pip install pyarrow>=22.0.0 httpx>=0.27.0
```

---

## Quick Start

### Direct Mode (In-Process)

Use when the Query API is in the same process (e.g., dfe-control-plane services):

```python
from dfe_engine.query import QueryClient

# Create client in direct mode
client = QueryClient(direct=True)

# Execute query - returns PyArrow Table
table = client.query("clickhouse:default", "SELECT * FROM logs LIMIT 100")
print(f"Rows: {table.num_rows}, Columns: {table.column_names}")

# Convert to pandas DataFrame (zero-copy)
df = client.query_df("clickhouse:default", "SELECT * FROM logs LIMIT 100")
df.groupby("level").count()
```

### HTTP Mode (External Consumer)

Use when consuming the API over HTTP:

```python
from dfe_engine.query import QueryClient

# Create client with API URL
client = QueryClient(base_url="http://localhost:8000")

# Same API as direct mode
table = client.query("clickhouse:default", "SELECT * FROM logs")
df = client.query_df("clickhouse:default", "SELECT * FROM logs")
```

---

## API Reference

### QueryClient

```python
class QueryClient:
    def __init__(
        self,
        base_url: str | None = None,      # API URL for HTTP mode
        direct: bool = False,              # Use in-process execution
        timeout_seconds: int = 30,         # Default query timeout
    ): ...
```

### Methods

#### `query(datasource, sql, params?, timeout_seconds?) -> pa.Table`

Execute query and return Arrow Table.

```python
# Simple query
table = client.query("clickhouse:default", "SELECT * FROM events")

# With parameters (prevents SQL injection)
table = client.query(
    "clickhouse:default",
    "SELECT * FROM events WHERE org_id = {org:String} AND level = {level:String}",
    params={"org": "acme", "level": "ERROR"},
)

# With custom timeout
table = client.query(
    "clickhouse:default",
    "SELECT * FROM huge_table",
    timeout_seconds=120,
)
```

#### `query_df(datasource, sql, params?, timeout_seconds?) -> pd.DataFrame`

Execute query and return pandas DataFrame (zero-copy from Arrow).

```python
df = client.query_df("clickhouse:default", "SELECT * FROM logs")

# DataFrame operations
df.groupby("level").agg({"count": "sum"})
df.to_parquet("output.parquet")
```

#### `query_with_explain(datasource, sql, params?, timeout_seconds?, parallel?) -> QueryResult`

Execute query with EXPLAIN plan.

```python
result = client.query_with_explain(
    "clickhouse:default",
    "SELECT * FROM logs WHERE level = 'ERROR'",
    parallel=True,  # Run query and EXPLAIN concurrently
)

# Access results
print(f"Rows: {result.num_rows}")
print(f"Duration: {result.metadata.query_duration_ms}ms")

# Access EXPLAIN plan
for step in result.explain.steps:
    print(f"{step.step_type}: {step.description}")

# Warnings about query performance
for warning in result.explain.warnings:
    print(f"Warning: {warning}")

# Convert to DataFrame
df = result.to_pandas()
```

#### `query_batches(datasource, sql, params?, batch_size?) -> Iterator[pa.RecordBatch]`

Stream large results in batches (memory efficient).

```python
# Process large table without loading all into memory
for batch in client.query_batches(
    "clickhouse:default",
    "SELECT * FROM huge_table",
    batch_size=10_000,
):
    # Process each batch
    df_batch = batch.to_pandas()
    process(df_batch)
```

---

## QueryResult

Returned by `query_with_explain()`.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `table` | `pa.Table` | Arrow Table with results |
| `metadata` | `QueryMetadata` | Execution metadata |
| `explain` | `ExplainPlan` | Query execution plan |
| `num_rows` | `int` | Number of rows |
| `num_columns` | `int` | Number of columns |
| `schema` | `pa.Schema` | Arrow schema |
| `column_names` | `list[str]` | Column names |

### Export Methods

```python
result = client.query_with_explain("clickhouse:default", sql)

# Arrow formats
table = result.to_arrow()           # PyArrow Table
ipc_bytes = result.to_arrow_ipc()   # Arrow IPC bytes

# Pandas
df = result.to_pandas()

# Python native
rows = result.to_pylist()           # List of dicts
cols = result.to_pydict()           # Dict of lists

# Text formats
json_str = result.to_json()
csv_str = result.to_csv()

# File export
result.to_parquet("output.parquet")

# Iteration
for batch in result.iter_batches(batch_size=5000):
    process(batch)

for row in result.iter_rows():
    process(row)
```

---

## ExplainPlan

Query execution plan from EXPLAIN.

### Structure

```python
@dataclass
class ExplainPlan:
    steps: list[ExplainStep]           # Execution steps
    total_estimated_cost: float | None # Estimated query cost
    total_estimated_rows: int | None   # Estimated row count
    warnings: list[str]                # Performance warnings
    raw_plan: str | None               # Original EXPLAIN output

@dataclass
class ExplainStep:
    step_type: ExplainStepType         # READ, FILTER, AGGREGATE, etc.
    description: str                   # Step description
    estimated_rows: int | None         # Estimated rows for this step
    estimated_cost: float | None       # Estimated cost
    actual_rows: int | None            # Actual rows (if available)
    actual_time_ms: float | None       # Actual time (if available)
    details: dict | None               # Step-specific details
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

### Example Usage

```python
result = client.query_with_explain(
    "clickhouse:default",
    """
    SELECT org_id, count() as cnt
    FROM events
    WHERE timestamp > now() - INTERVAL 1 DAY
    GROUP BY org_id
    ORDER BY cnt DESC
    LIMIT 10
    """,
    parallel=True,
)

# Analyze the plan
print(f"Estimated rows: {result.explain.total_estimated_rows}")

for step in result.explain.steps:
    if step.step_type == ExplainStepType.READ:
        print(f"Reading from: {step.details.get('table')}")
    elif step.step_type == ExplainStepType.FILTER:
        print(f"Filter: {step.description}")

# Check for warnings
if result.explain.warnings:
    print("Performance warnings:")
    for w in result.explain.warnings:
        print(f"  - {w}")

# Raw plan for debugging
print(result.explain.raw_plan)
```

---

## Parallel EXPLAIN

When `parallel=True`, the query and EXPLAIN run concurrently:

```python
# Sequential (default): query runs, then EXPLAIN
result = client.query_with_explain(datasource, sql, parallel=False)
# Total time ≈ query_time + explain_time

# Parallel: query and EXPLAIN run concurrently
result = client.query_with_explain(datasource, sql, parallel=True)
# Total time ≈ max(query_time, explain_time)
```

Use parallel mode when:

- You need the EXPLAIN plan for analysis
- Query execution time is non-trivial
- You want to minimize total latency

---

## Datasource URIs

| URI | Backend | Notes |
|-----|---------|-------|
| `clickhouse:default` | ClickHouse | Default target from config |
| `clickhouse:analytics` | ClickHouse | Named target |
| `postgres:main` | PostgreSQL | Default PostgreSQL |
| `prometheus:metrics` | Prometheus | PromQL queries |

---

## Error Handling

```python
from dfe_engine.query import QueryClient
import httpx

client = QueryClient(base_url="http://localhost:8000")

try:
    table = client.query("clickhouse:default", "SELECT * FROM nonexistent")
except httpx.HTTPStatusError as e:
    if e.response.status_code == 400:
        print(f"Query error: {e.response.text}")
    elif e.response.status_code == 504:
        print("Query timed out")
    else:
        raise
except httpx.ConnectError:
    print("Cannot connect to Query API")
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

### 2. Use Parameterized Queries

```python
# BAD: String interpolation (SQL injection risk, no caching)
sql = f"SELECT * FROM events WHERE org_id = '{org_id}'"

# GOOD: Parameters (safe, enables query caching)
table = client.query(
    "clickhouse:default",
    "SELECT * FROM events WHERE org_id = {org:String}",
    params={"org": org_id},
)
```

### 3. Stream Large Results

```python
# BAD: Load everything into memory
table = client.query("clickhouse:default", "SELECT * FROM huge_table")
df = table.to_pandas()  # Memory spike

# GOOD: Stream in batches
for batch in client.query_batches(
    "clickhouse:default",
    "SELECT * FROM huge_table",
    batch_size=50_000,
):
    process_batch(batch.to_pandas())
```

### 4. Use Arrow Native Operations

```python
# GOOD: Stay in Arrow for computations
import pyarrow.compute as pc

table = client.query("clickhouse:default", "SELECT * FROM events")

# Filter in Arrow (fast)
filtered = table.filter(pc.field("level") == "ERROR")

# Aggregate in Arrow
counts = pc.value_counts(table["level"])

# Only convert to pandas when needed for pandas-specific operations
df = filtered.to_pandas()
```

---

## Integration Examples

### FastAPI Service

```python
from fastapi import FastAPI, Depends
from dfe_engine.query import QueryClient

app = FastAPI()

def get_query_client() -> QueryClient:
    return QueryClient(direct=True)

@app.get("/events/{org_id}")
async def get_events(
    org_id: str,
    client: QueryClient = Depends(get_query_client),
):
    result = client.query_with_explain(
        "clickhouse:default",
        "SELECT * FROM events WHERE org_id = {org:String} LIMIT 100",
        params={"org": org_id},
        parallel=True,
    )
    return {
        "data": result.to_pylist(),
        "metadata": {
            "rows": result.num_rows,
            "duration_ms": result.metadata.query_duration_ms,
        },
    }
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
    sql: str,
    datasource: str = "clickhouse:default",
    explain: bool = False,
):
    client = QueryClient(direct=True)

    if explain:
        result = client.query_with_explain(datasource, sql, parallel=True)
        console.print(f"[dim]Duration: {result.metadata.query_duration_ms}ms[/dim]")
        console.print(f"[dim]Plan:[/dim]")
        for step in result.explain.steps:
            console.print(f"  {step.step_type}: {step.description}")
        table_data = result.table
    else:
        table_data = client.query(datasource, sql)

    # Display as rich table
    rich_table = Table()
    for col in table_data.column_names:
        rich_table.add_column(col)
    for row in table_data.to_pylist()[:50]:
        rich_table.add_row(*[str(v) for v in row.values()])
    console.print(rich_table)
```

---

## References

- [Apache Arrow Python Documentation](https://arrow.apache.org/docs/python/index.html)
- [PyArrow on PyPI](https://pypi.org/project/pyarrow/) (v22.0.0)
- [Apache Arrow 22.0.0 Release Notes](https://arrow.apache.org/release/22.0.0.html)
