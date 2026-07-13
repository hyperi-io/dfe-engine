# DFE Query API - Rust SDK

**Version:** 2.0.0
**Last Updated:** 2026-07-13

This document specifies how to consume the DFE Query API from Rust applications.

---

## Overview

The Query API provides a **secure, label-based interface** for querying multiple datasources. Key security features:

- **No raw SQL from clients** - Queries are referenced by label, SQL is defined server-side
- **Mandatory tenant isolation** - `_org_id` injected from JWT, cannot be overridden
- **Role-based access control** - Queries can require specific roles/permissions
- **Native JSON wire format** - responses are a single JSON body, deserialized with `serde`

The wire format is plain JSON, not Apache Arrow - there is no Arrow IPC stream to
decode over the wire. If you want Arrow-native columnar processing (DataFusion,
Polars, `arrow` compute kernels) once the rows are in your process, convert the
JSON rows to Arrow locally - see [Integration with DataFusion](#integration-with-datafusion)
and [Integration with Polars](#integration-with-polars) below for that optional,
client-side pattern.

---

## Requirements

### Minimum Versions

| Crate | Version | Purpose |
|-------|---------|---------|
| `reqwest` | ≥0.12.0 | HTTP client |
| `tokio` | ≥1.40.0 | Async runtime |
| `serde` | ≥1.0.0 | JSON serialization |
| `serde_json` | ≥1.0.0 | JSON parsing |
| `thiserror` | ≥2.0.0 | Error types |
| Rust | ≥1.80.0 | MSRV |

Optional, only if you want to convert results to Arrow client-side for
DataFusion/Polars integration (see below): `arrow`, `arrow-json`, `datafusion`,
`polars`. Check current versions before adding these - they are not required
by the core client.

### Cargo.toml

```toml
[dependencies]
# HTTP client
reqwest = { version = "0.12", features = ["json"] }

# Async runtime
tokio = { version = "1.40", features = ["full"] }

# Serialization
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"

# Error handling
thiserror = "2.0"
```

---

## Quick Start

```rust
use dfe_query::QueryClient;
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Create client
    let client = QueryClient::new("http://localhost:8000");

    // Execute query by label with parameters
    let result = client
        .query(
            "analytics/user_activity",
            Some(json!({ "event_types": ["login", "logout"] })),
        )
        .await?;

    println!("Rows: {}", result.num_rows());
    println!("Columns: {:?}", result.columns());

    // Iterate over rows (each row is a JSON object)
    for row in result.rows() {
        println!("{:?}", row);
    }

    Ok(())
}
```

---

## Implementation

### Types (`src/types.rs`)

```rust
use serde::{Deserialize, Serialize};

/// A single result row - a JSON object keyed by column name.
pub type Row = serde_json::Map<String, serde_json::Value>;

/// Raw JSON response body returned by the Query API.
#[derive(Debug, Clone, Deserialize)]
pub struct QueryResponseBody {
    pub rows: Vec<Row>,
    pub columns: Vec<String>,
    pub row_count: usize,
    pub query_duration_ms: u64,
    #[serde(default)]
    pub has_more: bool,
    pub next_offset: Option<usize>,
    pub request_id: Option<String>,
    #[serde(default)]
    pub explain: Option<ExplainPlan>,
}

/// Query execution metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryMetadata {
    pub row_count: usize,
    pub query_duration_ms: u64,
    pub query_label: String,
    pub datasource: String,
    #[serde(default)]
    pub truncated: bool,
    #[serde(default)]
    pub cached: bool,
    pub explain_duration_ms: Option<u64>,
    pub request_id: Option<String>,
    // Pagination info
    #[serde(default)]
    pub has_more: bool,
    pub next_cursor: Option<String>,
    pub next_offset: Option<usize>,
    pub total_count: Option<usize>,
}

/// EXPLAIN step type.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExplainStepType {
    Read,
    Filter,
    Aggregate,
    Sort,
    Join,
    Projection,
    Limit,
    Union,
    Unknown,
}

/// Single step in EXPLAIN plan.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExplainStep {
    pub step_type: ExplainStepType,
    pub description: String,
    pub estimated_rows: Option<u64>,
    pub estimated_cost: Option<f64>,
    pub actual_rows: Option<u64>,
    pub actual_time_ms: Option<f64>,
    #[serde(default)]
    pub details: Option<serde_json::Value>,
}

/// Query execution plan.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExplainPlan {
    pub steps: Vec<ExplainStep>,
    pub total_estimated_cost: Option<f64>,
    pub total_estimated_rows: Option<u64>,
    #[serde(default)]
    pub warnings: Vec<String>,
    pub raw_plan: Option<String>,
}

/// Query request payload.
#[derive(Debug, Serialize)]
pub struct QueryRequest {
    /// Query label (e.g., "analytics/user_activity")
    pub query: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub params: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub options: Option<QueryOptions>,
}

/// Query options.
#[derive(Debug, Default, Clone, Serialize)]
pub struct QueryOptions {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub limit: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub offset: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cursor: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub after_key: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order_by: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order_dir: Option<String>,  // "asc" or "desc"
    #[serde(skip_serializing_if = "Option::is_none")]
    pub time_from: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub time_to: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timeout_seconds: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub include_explain: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parallel: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub store: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cache: Option<bool>,
}
```

### Error Types (`src/error.rs`)

```rust
use thiserror::Error;

#[derive(Error, Debug)]
pub enum QueryError {
    #[error("HTTP error: {status} - {body}")]
    Http { status: u16, body: String },

    #[error("Network error: {0}")]
    Network(#[from] reqwest::Error),

    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("Invalid response: {0}")]
    InvalidResponse(String),

    #[error("Query timeout")]
    Timeout,
}

pub type Result<T> = std::result::Result<T, QueryError>;
```

### Query Result (`src/result.rs`)

```rust
use crate::types::{ExplainPlan, QueryMetadata, Row};

/// Query result containing JSON rows and metadata.
#[derive(Debug, Clone)]
pub struct QueryResult {
    rows: Vec<Row>,
    columns: Vec<String>,
    metadata: QueryMetadata,
    explain: Option<ExplainPlan>,
}

impl QueryResult {
    pub(crate) fn new(
        rows: Vec<Row>,
        columns: Vec<String>,
        metadata: QueryMetadata,
        explain: Option<ExplainPlan>,
    ) -> Self {
        Self {
            rows,
            columns,
            metadata,
            explain,
        }
    }

    /// Get the result rows (each a JSON object keyed by column name).
    pub fn rows(&self) -> &[Row] {
        &self.rows
    }

    /// Consume the result and return owned rows.
    pub fn into_rows(self) -> Vec<Row> {
        self.rows
    }

    /// Get column names.
    pub fn columns(&self) -> &[String] {
        &self.columns
    }

    /// Get the number of rows.
    pub fn num_rows(&self) -> usize {
        self.rows.len()
    }

    /// Get the number of columns.
    pub fn num_columns(&self) -> usize {
        self.columns.len()
    }

    /// Get query metadata.
    pub fn metadata(&self) -> &QueryMetadata {
        &self.metadata
    }

    /// Get EXPLAIN plan (if requested).
    pub fn explain(&self) -> Option<&ExplainPlan> {
        self.explain.as_ref()
    }

    /// Iterate over rows.
    pub fn iter(&self) -> impl Iterator<Item = &Row> {
        self.rows.iter()
    }
}

impl IntoIterator for QueryResult {
    type Item = Row;
    type IntoIter = std::vec::IntoIter<Row>;

    fn into_iter(self) -> Self::IntoIter {
        self.rows.into_iter()
    }
}
```

### Client (`src/client.rs`)

```rust
use reqwest::Client;
use std::time::Duration;

use crate::error::{QueryError, Result};
use crate::result::QueryResult;
use crate::types::{QueryMetadata, QueryOptions, QueryRequest, QueryResponseBody};

/// Query API client.
#[derive(Clone)]
pub struct QueryClient {
    base_url: String,
    client: Client,
    default_timeout: Duration,
}

impl QueryClient {
    /// Create a new client with default settings.
    pub fn new(base_url: impl Into<String>) -> Self {
        Self {
            base_url: base_url.into(),
            client: Client::new(),
            default_timeout: Duration::from_secs(30),
        }
    }

    /// Create client with custom HTTP client.
    pub fn with_client(base_url: impl Into<String>, client: Client) -> Self {
        Self {
            base_url: base_url.into(),
            client,
            default_timeout: Duration::from_secs(30),
        }
    }

    /// Set default timeout.
    pub fn timeout(mut self, timeout: Duration) -> Self {
        self.default_timeout = timeout;
        self
    }

    /// Execute a query by label.
    pub async fn query(
        &self,
        query_label: &str,
        params: Option<serde_json::Value>,
    ) -> Result<QueryResult> {
        self.query_with_options(query_label, params, None).await
    }

    /// Execute a query with options.
    pub async fn query_with_options(
        &self,
        query_label: &str,
        params: Option<serde_json::Value>,
        options: Option<QueryOptions>,
    ) -> Result<QueryResult> {
        let request = QueryRequest {
            query: query_label.to_string(),
            params,
            options,
        };

        let response = self
            .client
            .post(format!("{}/api/v1/query", self.base_url))
            .json(&request)
            .timeout(self.default_timeout + Duration::from_secs(10))
            .send()
            .await?;

        if !response.status().is_success() {
            let status = response.status().as_u16();
            let body = response.text().await.unwrap_or_default();
            return Err(QueryError::Http { status, body });
        }

        // Response is a single native JSON body - rows, columns and
        // metadata all arrive together, no separate wire schema to parse.
        let body: QueryResponseBody = response.json().await?;

        let explain = body.explain.clone();
        let metadata = QueryMetadata {
            row_count: body.row_count,
            query_duration_ms: body.query_duration_ms,
            query_label: query_label.to_string(),
            datasource: "clickhouse".to_string(),
            truncated: false,
            cached: false,
            explain_duration_ms: None,
            request_id: body.request_id,
            has_more: body.has_more,
            next_cursor: None,
            next_offset: body.next_offset,
            total_count: None,
        };

        Ok(QueryResult::new(body.rows, body.columns, metadata, explain))
    }

    /// Execute query with EXPLAIN plan.
    pub async fn query_with_explain(
        &self,
        query_label: &str,
        params: Option<serde_json::Value>,
        parallel: bool,
    ) -> Result<QueryResult> {
        let options = QueryOptions {
            include_explain: Some(true),
            parallel: Some(parallel),
            ..Default::default()
        };

        self.query_with_options(query_label, params, Some(options)).await
    }
}
```

### Library Root (`src/lib.rs`)

```rust
//! DFE Query API Client for Rust
//!
//! This crate provides a client for the DFE Query API. The wire format is
//! native JSON - queries are referenced by label, SQL is defined server-side.
//!
//! # Example
//!
//! ```rust,no_run
//! use dfe_query::QueryClient;
//! use serde_json::json;
//!
//! #[tokio::main]
//! async fn main() -> Result<(), Box<dyn std::error::Error>> {
//!     let client = QueryClient::new("http://localhost:8000");
//!
//!     // Query by label with parameters
//!     let result = client
//!         .query("analytics/user_activity", Some(json!({ "limit": 100 })))
//!         .await?;
//!
//!     for row in result.rows() {
//!         println!("{:?}", row);
//!     }
//!
//!     Ok(())
//! }
//! ```

mod client;
mod error;
mod result;
mod types;

pub use client::QueryClient;
pub use error::{QueryError, Result};
pub use result::QueryResult;
pub use types::{
    ExplainPlan, ExplainStep, ExplainStepType,
    QueryMetadata, QueryOptions, QueryRequest, Row,
};
```

---

## Usage Examples

### Basic Query

```rust
use dfe_query::QueryClient;
use serde_json::json;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    // Query by label
    let result = client
        .query("analytics/user_activity", None)
        .await?;

    println!("Query returned {} rows in {}ms",
        result.num_rows(),
        result.metadata().query_duration_ms
    );

    // Access JSON row data
    for row in result.rows() {
        if let Some(ts) = row.get("timestamp") {
            println!("timestamp = {}", ts);
        }
    }

    Ok(())
}
```

### With Parameters

```rust
use dfe_query::QueryClient;
use serde_json::json;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    // Parameters are validated against server-side schema
    let result = client
        .query(
            "hunts/active_threats",
            Some(json!({
                "severities": ["critical", "high"]
            })),
        )
        .await?;

    println!("Found {} active threats", result.num_rows());

    Ok(())
}
```

### Query with Options

```rust
use dfe_query::{QueryClient, QueryOptions};
use serde_json::json;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    let options = QueryOptions {
        limit: Some(500),
        time_from: Some("2024-01-01T00:00:00Z".to_string()),
        time_to: Some("2024-01-31T23:59:59Z".to_string()),
        timeout_seconds: Some(120),
        ..Default::default()
    };

    let result = client
        .query_with_options(
            "analytics/user_activity",
            Some(json!({ "event_types": ["login", "logout"] })),
            Some(options),
        )
        .await?;

    println!("Found {} events", result.num_rows());

    Ok(())
}
```

### Query with EXPLAIN

```rust
use dfe_query::{QueryClient, ExplainStepType};
use serde_json::json;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    let result = client
        .query_with_explain(
            "analytics/top_orgs",
            Some(json!({ "days": 7 })),
            true, // parallel execution
        )
        .await?;

    println!("Query: {}ms", result.metadata().query_duration_ms);

    // Analyze execution plan
    if let Some(explain) = result.explain() {
        println!("\nExecution Plan:");
        for step in &explain.steps {
            let rows = step.estimated_rows
                .map(|r| format!("~{} rows", r))
                .unwrap_or_default();
            println!("  {:?}: {} {}", step.step_type, step.description, rows);
        }

        // Check warnings
        if !explain.warnings.is_empty() {
            println!("\nWarnings:");
            for warning in &explain.warnings {
                println!("  - {}", warning);
            }
        }

        // Raw plan for debugging
        if let Some(raw) = &explain.raw_plan {
            println!("\nRaw Plan:\n{}", raw);
        }
    }

    Ok(())
}
```

### Integration with DataFusion

The wire format is JSON, not Arrow, so there is no `RecordBatch` straight off
the response. If you want DataFusion-style SQL on the result set, convert the
JSON rows to Arrow locally (this is a client-side choice, not something the
API provides):

```rust
use arrow_json::ReaderBuilder;
use datafusion::prelude::*;
use dfe_query::QueryClient;
use std::io::Cursor;
use std::sync::Arc;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = QueryClient::new("http://localhost:8000");

    let result = client.query("analytics/all_events", None).await?;

    // Local JSON -> Arrow conversion (optional; only needed if you want
    // DataFusion/Arrow-native processing after the fact).
    let ndjson: String = result
        .rows()
        .iter()
        .map(|row| serde_json::to_string(row).unwrap())
        .collect::<Vec<_>>()
        .join("\n");

    let schema = arrow_json::reader::infer_json_schema_from_seekable(
        &mut Cursor::new(ndjson.as_bytes()),
        None,
    )?;
    let mut reader = ReaderBuilder::new(Arc::new(schema)).build(Cursor::new(ndjson.as_bytes()))?;
    let batch = reader.next().transpose()?.expect("non-empty result");

    let ctx = SessionContext::new();
    ctx.register_batch("events", batch)?;

    let df = ctx
        .sql("SELECT org_id, COUNT(*) as cnt FROM events GROUP BY org_id")
        .await?;

    df.show().await?;

    Ok(())
}
```

### Integration with Polars

Polars can read newline-delimited JSON directly, so converting the row set is
a single step:

```rust
use dfe_query::{QueryClient, QueryOptions};
use polars::prelude::*;
use std::io::Cursor;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = QueryClient::new("http://localhost:8000");

    let options = QueryOptions {
        limit: Some(10_000),
        ..Default::default()
    };

    let result = client
        .query_with_options("analytics/all_events", None, Some(options))
        .await?;

    let ndjson: String = result
        .rows()
        .iter()
        .map(|row| serde_json::to_string(row).unwrap())
        .collect::<Vec<_>>()
        .join("\n");

    let df = JsonLineReader::new(Cursor::new(ndjson.as_bytes())).finish()?;

    let grouped = df
        .lazy()
        .group_by([col("org_id")])
        .agg([col("*").count().alias("count")])
        .sort("count", SortOptions::default().with_order_descending(true))
        .collect()?;

    println!("{}", grouped);

    Ok(())
}
```

### Processing Large Results Without Loading Everything

The response is a single JSON body per request - there is no server-side
streaming of batches. To process a large result set without loading it all
into memory at once, page through it (see [Pagination](#pagination)) and
process each page as it arrives:

```rust
use dfe_query::{QueryClient, QueryOptions};
use std::time::Duration;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000")
        .timeout(Duration::from_secs(300)); // 5 minute timeout

    let mut options = QueryOptions {
        limit: Some(50_000),
        ..Default::default()
    };

    let mut total_processed = 0;
    loop {
        let result = client
            .query_with_options("analytics/all_events", None, Some(options.clone()))
            .await?;

        for row in result.rows() {
            process_row(row);
        }
        total_processed += result.num_rows();

        if !result.metadata().has_more {
            break;
        }
        options.offset = result.metadata().next_offset.map(|o| o as u32);
    }

    println!("Total: {} rows", total_processed);

    Ok(())
}

fn process_row(row: &dfe_query::Row) {
    // Your processing logic here
}
```

### Error Handling

```rust
use dfe_query::{QueryClient, QueryError};

#[tokio::main]
async fn main() {
    let client = QueryClient::new("http://localhost:8000");

    match client.query("analytics/user_activity", None).await {
        Ok(result) => {
            println!("Got {} rows", result.num_rows());
        }
        Err(QueryError::Http { status, body }) => {
            match status {
                400 => eprintln!("Invalid parameters: {}", body),
                401 => eprintln!("Unauthorized - check credentials"),
                403 => eprintln!("Forbidden - missing required role"),
                404 => eprintln!("Query not found: {}", body),
                504 => eprintln!("Query timed out"),
                _ => eprintln!("HTTP error {}: {}", status, body),
            }
        }
        Err(QueryError::Network(e)) => {
            eprintln!("Network error: {}", e);
        }
        Err(QueryError::Json(e)) => {
            eprintln!("JSON deserialization error: {}", e);
        }
        Err(e) => {
            eprintln!("Error: {}", e);
        }
    }
}
```

---

## Pagination

The Query API supports three pagination modes.

### Offset-Based Pagination

```rust
use dfe_query::{QueryClient, QueryOptions};

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    // First page
    let options = QueryOptions {
        limit: Some(100),
        ..Default::default()
    };
    let result = client
        .query_with_options("analytics/user_activity", None, Some(options))
        .await?;

    // Second page
    if result.metadata().has_more {
        let options = QueryOptions {
            limit: Some(100),
            offset: result.metadata().next_offset.map(|o| o as u32),
            ..Default::default()
        };
        let page2 = client
            .query_with_options("analytics/user_activity", None, Some(options))
            .await?;
    }

    Ok(())
}
```

### Cursor-Based Pagination

```rust
use dfe_query::{QueryClient, QueryOptions};

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    // First page
    let mut options = QueryOptions {
        limit: Some(100),
        ..Default::default()
    };
    let mut result = client
        .query_with_options("analytics/user_activity", None, Some(options.clone()))
        .await?;

    let mut all_rows = result.num_rows();

    // Fetch all pages using cursor
    while result.metadata().has_more {
        options.cursor = result.metadata().next_cursor.clone();

        result = client
            .query_with_options("analytics/user_activity", None, Some(options.clone()))
            .await?;

        all_rows += result.num_rows();
    }

    println!("Total rows: {}", all_rows);
    Ok(())
}
```

### Keyset-Based Pagination

```rust
use dfe_query::{QueryClient, QueryOptions};
use serde_json::json;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    // First page (sorted by timestamp descending)
    let mut options = QueryOptions {
        limit: Some(100),
        order_by: Some("timestamp".to_string()),
        order_dir: Some("desc".to_string()),
        ..Default::default()
    };

    let mut result = client
        .query_with_options("analytics/user_activity", None, Some(options.clone()))
        .await?;

    // Next pages using last timestamp as after_key
    while result.metadata().has_more && result.num_rows() > 0 {
        let last_timestamp = get_last_timestamp(&result);

        options.after_key = Some(json!(last_timestamp));

        result = client
            .query_with_options("analytics/user_activity", None, Some(options.clone()))
            .await?;
    }

    Ok(())
}

fn get_last_timestamp(result: &dfe_query::QueryResult) -> String {
    // Extract "timestamp" from the last row
    result
        .rows()
        .last()
        .and_then(|row| row.get("timestamp"))
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string()
}
```

---

## Storage Listing

Query API provides built-in queries for listing files in S3, MinIO, and local filesystems.

### S3 Bucket Listing

```rust
use dfe_query::QueryClient;
use serde_json::json;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    let result = client
        .query(
            "storage/s3_list",
            Some(json!({
                "bucket": "my-bucket",
                "prefix": "logs/2024/"
            })),
        )
        .await?;

    println!("Found {} items", result.num_rows());

    // Columns: name, path, type, size, modified, etag, storage_class, content_type
    for row in result.rows() {
        println!("{:?}", row);
    }

    Ok(())
}
```

### MinIO Listing

```rust
use dfe_query::QueryClient;
use serde_json::json;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    // Same API as S3
    let result = client
        .query(
            "storage/minio_list",
            Some(json!({
                "bucket": "my-bucket",
                "prefix": "data/"
            })),
        )
        .await?;

    Ok(())
}
```

### Filesystem Listing

```rust
use dfe_query::QueryClient;
use serde_json::json;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    let result = client
        .query(
            "storage/file_list",
            Some(json!({
                "path": "reports/2024",
                "recursive": true,
                "pattern": "*.json"
            })),
        )
        .await?;

    println!("Found {} files", result.num_rows());

    Ok(())
}
```

### Storage Listing Schema

All storage adapters return a consistent JSON schema:

| Column | Type | Description |
|--------|------|-------------|
| `name` | `string` | File or directory name |
| `path` | `string` | Full path within storage |
| `type` | `string` | `"file"` or `"directory"` |
| `size` | `number` | Size in bytes |
| `modified` | `string \| null` | Last modified time (ISO8601, UTC) |
| `etag` | `string \| null` | Object ETag (S3/MinIO) |
| `storage_class` | `string \| null` | Storage class |
| `content_type` | `string \| null` | MIME type |

### Paginating Storage Listings

```rust
use dfe_query::{QueryClient, QueryOptions};
use serde_json::json;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000");

    let params = json!({
        "bucket": "my-bucket",
        "prefix": "logs/"
    });

    let mut options = QueryOptions {
        limit: Some(1000),
        ..Default::default()
    };

    let mut result = client
        .query_with_options("storage/s3_list", Some(params.clone()), Some(options.clone()))
        .await?;

    let mut total_files = result.num_rows();

    while result.metadata().has_more {
        options.cursor = result.metadata().next_cursor.clone();

        result = client
            .query_with_options("storage/s3_list", Some(params.clone()), Some(options.clone()))
            .await?;

        total_files += result.num_rows();
    }

    println!("Total files: {}", total_files);

    Ok(())
}
```

---

## Performance Tips

### 1. Use Connection Pooling

```rust
use reqwest::Client;
use std::time::Duration;

let http_client = Client::builder()
    .pool_max_idle_per_host(10)
    .pool_idle_timeout(Duration::from_secs(30))
    .build()
    .unwrap();

let client = QueryClient::with_client("http://localhost:8000", http_client);
```

### 2. Process Pages Incrementally

```rust
// DON'T: Request one huge page and hold everything in memory
let options = QueryOptions { limit: Some(1_000_000), ..Default::default() };
let result = client.query_with_options("analytics/all_events", None, Some(options)).await?;

// DO: Page through results and process each page as it arrives (see Pagination)
let mut options = QueryOptions { limit: Some(50_000), ..Default::default() };
loop {
    let result = client
        .query_with_options("analytics/all_events", None, Some(options.clone()))
        .await?;
    for row in result.rows() {
        process(row);
    }
    if !result.metadata().has_more {
        break;
    }
    options.offset = result.metadata().next_offset.map(|o| o as u32);
}
```

---

## References

- [serde_json Documentation](https://docs.rs/serde_json/latest/serde_json/)
- [reqwest Documentation](https://docs.rs/reqwest/latest/reqwest/)
- [arrow-json crate](https://docs.rs/arrow-json/latest/arrow_json/) - optional, for local JSON -> Arrow conversion
- [DataFusion](https://github.com/apache/datafusion) - SQL query engine on Arrow (optional local integration)
- [Polars](https://github.com/pola-rs/polars) - DataFrame library with native NDJSON reader (optional local integration)
