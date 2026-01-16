# DFE Query API - Rust SDK

**Version:** 2.0.0
**Last Updated:** 2026-01-16

This document specifies how to consume the DFE Query API from Rust applications.

---

## Overview

The Query API provides a **secure, label-based interface** for querying multiple datasources. Key security features:

- **No raw SQL from clients** - Queries are referenced by label, SQL is defined server-side
- **Mandatory tenant isolation** - `_org_id` injected from JWT, cannot be overridden
- **Role-based access control** - Queries can require specific roles/permissions
- **Apache Arrow wire format** - Zero-copy deserialization via `arrow-rs`

Rust has first-class Arrow support via the `arrow-rs` crate, providing tight integration with the Rust data ecosystem (DataFusion, Polars, etc.).

---

## Requirements

### Minimum Versions

| Crate | Version | Purpose |
|-------|---------|---------|
| `arrow` | ≥57.0.0 | Arrow IPC deserialization |
| `arrow-ipc` | ≥57.0.0 | IPC stream reader |
| `reqwest` | ≥0.12.0 | HTTP client |
| `tokio` | ≥1.40.0 | Async runtime |
| `serde` | ≥1.0.0 | JSON serialization |
| `serde_json` | ≥1.0.0 | JSON parsing |
| Rust | ≥1.80.0 | MSRV |

### Cargo.toml

```toml
[dependencies]
# Arrow (enable IPC feature)
arrow = { version = "57", features = ["ipc"] }
arrow-ipc = "57"
arrow-schema = "57"
arrow-array = "57"

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

### Performance Optimization

Add to your `.cargo/config.toml` for optimal Arrow performance:

```toml
[target.x86_64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=native"]

[target.aarch64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=native"]
```

---

## Quick Start

```rust
use dfe_query::{QueryClient, QueryResult};
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
    println!("Columns: {:?}", result.column_names());

    // Iterate over batches
    for batch in result.batches() {
        println!("Batch with {} rows", batch.num_rows());
    }

    Ok(())
}
```

---

## Implementation

### Types (`src/types.rs`)

```rust
use arrow_schema::{DataType, Field, Schema};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

/// Column metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Column {
    pub name: String,
    pub data_type: String,
    pub nullable: bool,
}

impl From<&Field> for Column {
    fn from(field: &Field) -> Self {
        Self {
            name: field.name().clone(),
            data_type: format!("{:?}", field.data_type()),
            nullable: field.is_nullable(),
        }
    }
}

/// Query execution metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryMetadata {
    pub row_count: usize,
    pub query_duration_ms: u64,
    pub query_label: String,
    pub datasource: String,
    pub truncated: bool,
    pub cached: bool,
    pub explain_duration_ms: Option<u64>,
    // Pagination info
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
#[derive(Debug, Default, Serialize)]
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

    #[error("Arrow error: {0}")]
    Arrow(#[from] arrow::error::ArrowError),

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
use arrow_array::RecordBatch;
use arrow_schema::SchemaRef;
use std::sync::Arc;

use crate::types::{Column, ExplainPlan, QueryMetadata};

/// Query result containing Arrow data and metadata.
#[derive(Debug)]
pub struct QueryResult {
    batches: Vec<RecordBatch>,
    schema: SchemaRef,
    metadata: QueryMetadata,
    explain: Option<ExplainPlan>,
}

impl QueryResult {
    pub(crate) fn new(
        batches: Vec<RecordBatch>,
        schema: SchemaRef,
        metadata: QueryMetadata,
        explain: Option<ExplainPlan>,
    ) -> Self {
        Self {
            batches,
            schema,
            metadata,
            explain,
        }
    }

    /// Get the Arrow schema.
    pub fn schema(&self) -> SchemaRef {
        Arc::clone(&self.schema)
    }

    /// Get column metadata.
    pub fn columns(&self) -> Vec<Column> {
        self.schema
            .fields()
            .iter()
            .map(|f| Column::from(f.as_ref()))
            .collect()
    }

    /// Get column names.
    pub fn column_names(&self) -> Vec<&str> {
        self.schema.fields().iter().map(|f| f.name().as_str()).collect()
    }

    /// Get total row count across all batches.
    pub fn num_rows(&self) -> usize {
        self.batches.iter().map(|b| b.num_rows()).sum()
    }

    /// Get number of columns.
    pub fn num_columns(&self) -> usize {
        self.schema.fields().len()
    }

    /// Get query metadata.
    pub fn metadata(&self) -> &QueryMetadata {
        &self.metadata
    }

    /// Get EXPLAIN plan (if requested).
    pub fn explain(&self) -> Option<&ExplainPlan> {
        self.explain.as_ref()
    }

    /// Get record batches.
    pub fn batches(&self) -> &[RecordBatch] {
        &self.batches
    }

    /// Consume result and return batches.
    pub fn into_batches(self) -> Vec<RecordBatch> {
        self.batches
    }

    /// Iterate over batches.
    pub fn iter(&self) -> impl Iterator<Item = &RecordBatch> {
        self.batches.iter()
    }

    /// Convert to a single RecordBatch (concatenates all batches).
    pub fn to_batch(&self) -> Result<RecordBatch, arrow::error::ArrowError> {
        arrow::compute::concat_batches(&self.schema, &self.batches)
    }
}

impl IntoIterator for QueryResult {
    type Item = RecordBatch;
    type IntoIter = std::vec::IntoIter<RecordBatch>;

    fn into_iter(self) -> Self::IntoIter {
        self.batches.into_iter()
    }
}
```

### Client (`src/client.rs`)

```rust
use arrow_ipc::reader::StreamReader;
use reqwest::Client;
use std::io::Cursor;
use std::time::Duration;

use crate::error::{QueryError, Result};
use crate::result::QueryResult;
use crate::types::{ExplainPlan, QueryMetadata, QueryOptions, QueryRequest};

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

        // Parse metadata from headers
        let metadata = self.parse_metadata(&response, query_label)?;

        // Read Arrow IPC stream
        let bytes = response.bytes().await?;
        let cursor = Cursor::new(bytes);
        let reader = StreamReader::try_new(cursor, None)?;

        let schema = reader.schema();
        let batches: Vec<_> = reader.collect::<std::result::Result<_, _>>()?;

        // Extract EXPLAIN from schema metadata
        let explain = self.extract_explain(&schema);

        Ok(QueryResult::new(batches, schema, metadata, explain))
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

    fn parse_metadata(
        &self,
        response: &reqwest::Response,
        query_label: &str,
    ) -> Result<QueryMetadata> {
        let headers = response.headers();

        let row_count = headers
            .get("X-Row-Count")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);

        let query_duration_ms = headers
            .get("X-Query-Duration-Ms")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse().ok())
            .unwrap_or(0);

        let datasource = headers
            .get("X-Datasource")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("unknown")
            .to_string();

        let truncated = headers
            .get("X-Truncated")
            .and_then(|v| v.to_str().ok())
            .map(|v| v == "true")
            .unwrap_or(false);

        let cached = headers
            .get("X-Cached")
            .and_then(|v| v.to_str().ok())
            .map(|v| v == "true")
            .unwrap_or(false);

        let explain_duration_ms = headers
            .get("X-Explain-Duration-Ms")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse().ok());

        let has_more = headers
            .get("X-Has-More")
            .and_then(|v| v.to_str().ok())
            .map(|v| v == "true")
            .unwrap_or(false);

        let next_cursor = headers
            .get("X-Next-Cursor")
            .and_then(|v| v.to_str().ok())
            .map(String::from);

        let next_offset = headers
            .get("X-Next-Offset")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse().ok());

        let total_count = headers
            .get("X-Total-Count")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse().ok());

        Ok(QueryMetadata {
            row_count,
            query_duration_ms,
            query_label: query_label.to_string(),
            datasource,
            truncated,
            cached,
            explain_duration_ms,
            has_more,
            next_cursor,
            next_offset,
            total_count,
        })
    }

    fn extract_explain(&self, schema: &arrow_schema::SchemaRef) -> Option<ExplainPlan> {
        let metadata = schema.metadata();

        let steps_json = metadata.get("dfe:explain:steps")?;
        let data: serde_json::Value = serde_json::from_str(steps_json).ok()?;

        let steps = data
            .get("steps")
            .and_then(|s| serde_json::from_value(s.clone()).ok())
            .unwrap_or_default();

        let warnings = metadata
            .get("dfe:explain:warnings")
            .map(|w| w.split(',').map(String::from).collect())
            .unwrap_or_default();

        let raw_plan = metadata.get("dfe:explain:raw").cloned();

        let total_estimated_cost = metadata
            .get("dfe:explain:estimated_cost")
            .and_then(|v| v.parse().ok());

        Some(ExplainPlan {
            steps,
            total_estimated_cost,
            total_estimated_rows: None,
            warnings,
            raw_plan,
        })
    }
}
```

### Library Root (`src/lib.rs`)

```rust
//! DFE Query API Client for Rust
//!
//! This crate provides a client for the DFE Query API with Apache Arrow
//! as the wire format. Queries are referenced by label - SQL is defined
//! server-side.
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
//!     for batch in result.batches() {
//!         println!("Batch: {} rows", batch.num_rows());
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
    Column, ExplainPlan, ExplainStep, ExplainStepType,
    QueryMetadata, QueryOptions, QueryRequest,
};

// Re-export Arrow types for convenience
pub use arrow_array::RecordBatch;
pub use arrow_schema::{DataType, Field, Schema, SchemaRef};
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

    // Access Arrow data
    for batch in result.batches() {
        let timestamp_col = batch.column(0);
        println!("First column has {} values", timestamp_col.len());
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
            println!("\n⚠️ Warnings:");
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

```rust
use arrow_array::RecordBatch;
use datafusion::prelude::*;
use dfe_query::QueryClient;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = QueryClient::new("http://localhost:8000");

    // Fetch data from Query API
    let result = client
        .query("analytics/all_events", None)
        .await?;

    // Create DataFusion context
    let ctx = SessionContext::new();

    // Register Arrow data as a table
    let batches: Vec<RecordBatch> = result.into_batches();
    let schema = batches[0].schema();

    ctx.register_batch("events", batches[0].clone())?;

    // Run SQL on the data locally
    let df = ctx
        .sql("SELECT org_id, COUNT(*) as cnt FROM events GROUP BY org_id")
        .await?;

    df.show().await?;

    Ok(())
}
```

### Integration with Polars

```rust
use dfe_query::{QueryClient, QueryOptions};
use polars::prelude::*;

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

    // Convert Arrow to Polars DataFrame
    let batches = result.into_batches();

    // Polars can read Arrow directly
    let df = DataFrame::try_from(batches)?;

    // Use Polars operations
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

### Streaming Large Results

```rust
use dfe_query::{QueryClient, QueryOptions};
use std::time::Duration;

#[tokio::main]
async fn main() -> dfe_query::Result<()> {
    let client = QueryClient::new("http://localhost:8000")
        .timeout(Duration::from_secs(300)); // 5 minute timeout

    let options = QueryOptions {
        limit: Some(1_000_000),
        ..Default::default()
    };

    let result = client
        .query_with_options("analytics/all_events", None, Some(options))
        .await?;

    // Process batches incrementally
    let mut total_processed = 0;
    for batch in result.batches() {
        // Process each batch without loading all into memory
        process_batch(batch);
        total_processed += batch.num_rows();

        if total_processed % 100_000 == 0 {
            println!("Processed {} rows...", total_processed);
        }
    }

    println!("Total: {} rows", total_processed);

    Ok(())
}

fn process_batch(batch: &arrow_array::RecordBatch) {
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
        Err(QueryError::Arrow(e)) => {
            eprintln!("Arrow deserialization error: {}", e);
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
        // Get last timestamp from result (implementation depends on schema)
        let last_timestamp = get_last_timestamp(&result);

        options.after_key = Some(json!(last_timestamp));

        result = client
            .query_with_options("analytics/user_activity", None, Some(options.clone()))
            .await?;
    }

    Ok(())
}

fn get_last_timestamp(result: &dfe_query::QueryResult) -> String {
    // Extract last timestamp from Arrow batch
    // Implementation depends on your schema
    "2024-01-15T12:00:00Z".to_string()
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
    for batch in result.batches() {
        // Process file listing
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

All storage adapters return consistent Arrow schema:

| Column | Type | Description |
|--------|------|-------------|
| `name` | `Utf8` | File or directory name |
| `path` | `Utf8` | Full path within storage |
| `type` | `Utf8` | `"file"` or `"directory"` |
| `size` | `Int64` | Size in bytes |
| `modified` | `Timestamp` | Last modified time (UTC) |
| `etag` | `Utf8` | Object ETag (S3/MinIO) |
| `storage_class` | `Utf8` | Storage class |
| `content_type` | `Utf8` | MIME type |

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

### 1. Enable SIMD Optimizations

```toml
# .cargo/config.toml
[target.x86_64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=native"]
```

### 2. Use Connection Pooling

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

### 3. Process Batches Incrementally

```rust
// DON'T: Load everything then process
let all_batches = result.into_batches();
let merged = concat_batches(&schema, &all_batches)?;

// DO: Process each batch as it comes
for batch in result.batches() {
    process(batch);
}
```

### 4. Use Arrow Compute Kernels

```rust
use arrow::compute;

let result = client.query("clickhouse:default", sql).await?;

for batch in result.batches() {
    // Use Arrow's optimized compute kernels
    let filtered = compute::filter(&batch, &predicate)?;
    let sorted = compute::sort(&batch, &sort_options)?;
}
```

---

## Feature Flags

The `arrow` crate provides optional features:

```toml
[dependencies.arrow]
version = "57"
features = [
    "ipc",           # Required: IPC stream reader
    "prettyprint",   # Optional: Pretty-print RecordBatch
    "chrono-tz",     # Optional: Timezone support
]
```

---

## References

- [arrow-rs GitHub](https://github.com/apache/arrow-rs) (v57.0.0+)
- [arrow crate on crates.io](https://crates.io/crates/arrow)
- [Arrow Rust Documentation](https://docs.rs/arrow/latest/arrow/)
- [Apache Arrow Rust 57.0.0 Release](https://arrow.apache.org/blog/2025/10/30/arrow-rs-57.0.0/)
- [DataFusion](https://github.com/apache/datafusion) - SQL query engine on Arrow
- [Polars](https://github.com/pola-rs/polars) - DataFrame library with Arrow backend
