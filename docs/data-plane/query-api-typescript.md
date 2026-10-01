# DFE Query API - TypeScript SDK

**Version:** 2.0.0
**Last Updated:** 2026-07-13

This document specifies how to consume the DFE Query API from TypeScript/JavaScript applications.

---

## Overview

The Query API provides a **secure, label-based interface** for querying multiple datasources. Key security features:

- **No raw SQL from clients** - Queries are referenced by label, SQL is defined server-side
- **Mandatory tenant isolation** - `_org_id` injected from JWT, cannot be overridden
- **Role-based access control** - Queries can require specific roles/permissions
- **Parameter validation** - All parameters validated against server-side schemas
- **Native JSON wire format** - plain `fetch`/JSON, no binary deserialization library required

---

## Requirements

### Minimum Versions

| Package | Version | Purpose |
|---------|---------|---------|
| `@tanstack/react-query` | ≥5.0.0 | Data fetching hooks (optional) |
| `@effect/schema` | ≥0.75.0 | Runtime type validation (optional) |
| TypeScript | ≥5.3 | Type definitions |
| Node.js | ≥20.0 | Runtime (for SSR) |

### Installation

```bash
# Optional: React Query integration
npm install @tanstack/react-query@^5.0.0

# Optional: Effect Schema integration
npm install @effect/schema@^0.75.0
```

---

## Quick Start

### Basic Usage

```typescript
import { QueryClient, QueryResult } from '@hyperi/query-client';

const client = new QueryClient({ baseUrl: 'http://localhost:8000' });

// Execute query by label with parameters
const result = await client.query('analytics/user_activity', {
  params: { eventTypes: ['login', 'logout'] },
  limit: 100,
});

console.log(`Rows: ${result.rowCount}`);
console.log(`Columns: ${result.columns}`);

// Access rows as typed objects
result.rows.forEach(row => {
  console.log(row.timestamp, row.eventType);
});
```

### With React Query

```typescript
import { useQuery } from '@hyperi/query-client/react';

function ThreatDashboard() {
  const { data, isLoading, error } = useQuery('hunts/active_threats', {
    params: { severities: ['critical', 'high'] },
    limit: 50,
  });

  if (isLoading) return <Loading />;
  if (error) return <Error error={error} />;

  return (
    <table>
      <thead>
        <tr>
          {data.columns.map(col => <th key={col}>{col}</th>)}
        </tr>
      </thead>
      <tbody>
        {data.rows.map((row, i) => (
          <tr key={i}>
            {data.columns.map(col => <td key={col}>{row[col]}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
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

```typescript
// Client sends:
const result = await client.query('hunts/active_threats', {
  params: { severities: ['critical', 'high'] },
  limit: 500,
});

// Server-side query definition (not visible to client):
// queries:
//   hunts/active_threats:
//     sql: |
//       SELECT alert_id, severity, timestamp
//       FROM {{ store }}.alerts
//       WHERE org_id = {{ _org_id }}  -- INJECTED, cannot be overridden
//       AND severity IN {{ severities | sql_array }}
//       LIMIT {{ limit }}
//     parameters:
//       severities:
//         type: array
//         items: string
//         required: true
//     tenant_isolated: true
//     required_roles: [analyst]
```

---

## API Reference

### QueryClient

```typescript
interface QueryClientConfig {
  baseUrl: string;              // API base URL
  timeout?: number;             // Default timeout in ms (default: 30000)
  headers?: Record<string, string>;  // Additional headers
}

class QueryClient {
  constructor(config: QueryClientConfig);

  query<T = Record<string, unknown>>(
    queryLabel: string,
    options?: QueryOptions,
  ): Promise<QueryResult<T>>;
}
```

### QueryOptions

```typescript
interface QueryOptions {
  params?: Record<string, unknown>;  // Query parameters
  limit?: number;                     // Max rows (clamped to server max)
  offset?: number;                    // Skip first N rows (offset pagination)
  orderBy?: string;                   // Column to order and page by (keyset pagination)
  orderDir?: 'asc' | 'desc';         // Sort direction (default: 'asc')
  afterKey?: string | number;         // orderBy value of the previous page's last row
  tiebreakBy?: string;                // Unique second column when orderBy is not unique
  afterTiebreak?: string | number;    // tiebreakBy value of that last row
  timeFrom?: string;                  // ISO8601 start time
  timeTo?: string;                    // ISO8601 end time
  timeoutMs?: number;                 // Query timeout
  store?: string;                     // Target store (if query allows)
  cache?: boolean;                    // Allow cached results (default: true)
}
```

### QueryResult

```typescript
interface QueryResult<T = Record<string, unknown>> {
  // Data access
  rows: T[];                          // Typed row objects (plain JSON)
  rowCount: number;                   // Number of rows
  columns: string[];                  // Column names

  // Metadata
  metadata: QueryMetadata;

  // Export methods
  toJSON(): string;
  toCSV(): string;
}

interface QueryMetadata {
  rowCount: number;
  queryDurationMs: number;
  queryLabel: string;
  datasource: string;
  store?: string;
  truncated: boolean;
  cached: boolean;
  requestId?: string;
  hasMore: boolean;                   // More pages available
  nextOffset?: number;                // Offset for next page
  totalCount?: number;                // Total rows (if available)
}
```

---

## Query Labels

Queries are referenced by labels in `namespace/name` format:

```typescript
// Analytics namespace
await client.query('analytics/user_activity');
await client.query('analytics/conversion_funnel');
await client.query('analytics/retention_cohorts');

// Hunts namespace
await client.query('hunts/active_threats');
await client.query('hunts/ioc_matches');
await client.query('hunts/anomaly_detection');

// System namespace (requires admin role)
await client.query('system/health_check');
await client.query('system/table_stats');
```

---

## React Query Integration

### Setup

```typescript
// providers.tsx
import { QueryClientProvider } from '@tanstack/react-query';
import { createQueryClient } from '@hyperi/query-client/react';

const queryClient = createQueryClient({
  baseUrl: process.env.NEXT_PUBLIC_API_URL!,
});

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  );
}
```

### useQuery Hook

```typescript
import { useQuery } from '@hyperi/query-client/react';

function ActivityTable() {
  const { data, isLoading, error, refetch } = useQuery(
    'analytics/user_activity',
    {
      params: { eventTypes: ['login', 'purchase'] },
      limit: 100,
      timeFrom: '2024-01-01T00:00:00Z',
    },
    {
      staleTime: 30_000,        // Consider fresh for 30s
      refetchOnWindowFocus: false,
    }
  );

  if (isLoading) return <Skeleton />;
  if (error) return <ErrorBanner error={error} />;

  return (
    <DataTable
      columns={data.columns}
      rows={data.rows}
      onRefresh={() => refetch()}
    />
  );
}
```

### useSuspenseQuery

```typescript
import { useSuspenseQuery } from '@hyperi/query-client/react';

function ThreatList() {
  // This will suspend until data is ready
  const { data } = useSuspenseQuery('hunts/active_threats', {
    params: { severities: ['critical'] },
  });

  return <ThreatTable threats={data.rows} />;
}

// Usage with Suspense boundary
function App() {
  return (
    <Suspense fallback={<Loading />}>
      <ThreatList />
    </Suspense>
  );
}
```

### useInfiniteQuery

```typescript
import { useInfiniteQuery } from '@hyperi/query-client/react';

function InfiniteEventList() {
  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery('analytics/all_events', {
    params: { date: '2024-01-15' },
    pageSize: 50,
  });

  return (
    <div>
      {data.pages.flatMap(page => page.rows).map(event => (
        <EventCard key={event.id} event={event} />
      ))}
      {hasNextPage && (
        <button
          onClick={() => fetchNextPage()}
          disabled={isFetchingNextPage}
        >
          {isFetchingNextPage ? 'Loading...' : 'Load More'}
        </button>
      )}
    </div>
  );
}
```

---

## Effect Integration

### Schema Validation

```typescript
import { Schema as S } from '@effect/schema';
import { query } from '@hyperi/query-client';

// Define row schema
const ThreatAlert = S.Struct({
  alertId: S.String,
  severity: S.Literal('critical', 'high', 'medium', 'low'),
  timestamp: S.DateFromString,
  description: S.String,
  source: S.String,
});

type ThreatAlert = S.Schema.Type<typeof ThreatAlert>;

// Query with schema validation
const result = await query<ThreatAlert>('hunts/active_threats', {
  params: { severities: ['critical', 'high'] },
  schema: ThreatAlert,  // Validates each row
});

// result.rows is typed as ThreatAlert[]
result.rows.forEach(alert => {
  console.log(alert.severity);  // Type-safe access
});
```

### Effect Error Handling

```typescript
import { Effect, pipe } from 'effect';
import { queryEffect } from '@hyperi/query-client/effect';

const program = pipe(
  queryEffect('hunts/active_threats', {
    params: { severities: ['critical'] },
  }),
  Effect.map(result => result.rows),
  Effect.catchTag('QueryNotFoundError', () =>
    Effect.succeed([])
  ),
  Effect.catchTag('AuthorizationError', error =>
    Effect.fail(new UnauthorizedError(error.message))
  ),
);

const threats = await Effect.runPromise(program);
```

---

## Error Handling

### Error Types

```typescript
import {
  QueryNotFoundError,
  ParameterValidationError,
  AuthorizationError,
  QueryTimeoutError,
  NetworkError,
} from '@hyperi/query-client';

try {
  const result = await client.query('hunts/active_threats', {
    params: { severities: ['critical'] },
  });
} catch (error) {
  if (error instanceof QueryNotFoundError) {
    console.error('Query not found:', error.queryLabel);
  } else if (error instanceof ParameterValidationError) {
    console.error('Invalid parameter:', error.param, error.message);
  } else if (error instanceof AuthorizationError) {
    console.error('Not authorized:', error.requiredRoles);
  } else if (error instanceof QueryTimeoutError) {
    console.error('Query timed out after', error.timeoutMs, 'ms');
  } else if (error instanceof NetworkError) {
    console.error('Network error:', error.message);
  } else {
    throw error;
  }
}
```

### Common Errors

| Error | Cause | Resolution |
|-------|-------|------------|
| `QueryNotFoundError` | Invalid query label | Check query label spelling |
| `ParameterValidationError` | Invalid or missing parameter | Check parameter schema |
| `AuthorizationError` | Missing required role/permission | Contact admin for access |
| `QueryTimeoutError` | Query execution timeout | Increase timeout or add filters |
| `NetworkError` | Connection failed | Check network/API availability |

---

## Pagination

The Query API supports two pagination modes, offset and keyset. It returns no EXPLAIN plans.

### Offset-Based Pagination

Traditional pagination with `limit` and `offset`. Simple but inefficient for deep pages.

```typescript
// First page
const page1 = await client.query('analytics/user_activity', { limit: 100 });

// Second page
const page2 = await client.query('analytics/user_activity', { limit: 100, offset: 100 });

// Using metadata
if (page1.metadata.hasMore) {
  const nextPage = await client.query('analytics/user_activity', {
    limit: 100,
    offset: page1.metadata.nextOffset,
  });
}
```

### Keyset-Based Pagination

Pagination on a sort key, stable under concurrent inserts. Best for time-series.

```typescript
// First page (sorted by timestamp descending, event_id breaking ties)
let result = await client.query('analytics/user_activity', {
  limit: 100,
  orderBy: 'timestamp',
  orderDir: 'desc',
  tiebreakBy: 'event_id',
});

// Next page: the last row's values
while (result.metadata.hasMore && result.rows.length > 0) {
  const last = result.rows[result.rows.length - 1];

  result = await client.query('analytics/user_activity', {
    limit: 100,
    orderBy: 'timestamp',
    orderDir: 'desc',
    tiebreakBy: 'event_id',
    afterKey: String(last.timestamp).replace(/Z$|[+-]00:00$/, ''),
    afterTiebreak: last.event_id,
  });
}
```

`afterKey` is compared as the `orderBy` column's type: send a timestamp as `2024-01-15T12:00:00` (with a fraction if the column has one). The key must be non-NULL, and without `tiebreakBy` it must be unique, or rows tying with the cursor are skipped. A view that declares its own `limit` parameter refuses keyset and offset paging.

### React Query Infinite Pagination

```typescript
import { useInfiniteQuery } from '@hyperi/query-client/react';

function InfiniteEventList() {
  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery('analytics/all_events', {
    params: { date: '2024-01-15' },
    pageSize: 50,
  });

  return (
    <div>
      {data.pages.flatMap(page => page.rows).map(event => (
        <EventCard key={event.id} event={event} />
      ))}
      {hasNextPage && (
        <button onClick={() => fetchNextPage()} disabled={isFetchingNextPage}>
          {isFetchingNextPage ? 'Loading...' : 'Load More'}
        </button>
      )}
    </div>
  );
}
```

### Pagination Recommendations

| Use Case | Mode | Reason |
|----------|------|--------|
| Time-series | Keyset | Stable under inserts, fast on indexed columns |
| Admin UIs | Offset | Jump to any page |
| Infinite scroll | Keyset | Constant cost per page at any depth |

---

## Storage Listing

Query API provides built-in queries for listing files in S3, MinIO, and local filesystems.

### S3 Bucket Listing

```typescript
const result = await client.query('storage/s3_list', {
  params: {
    bucket: 'my-bucket',
    prefix: 'logs/2024/',
  },
  limit: 1000,
});

// Columns: name, path, type, size, modified, etag, storageClass, contentType
result.rows.forEach(item => {
  console.log(`${item.type}: ${item.name} (${item.size} bytes)`);
});
```

### MinIO Listing

```typescript
// Same API as S3
const result = await client.query('storage/minio_list', {
  params: {
    bucket: 'my-bucket',
    prefix: 'data/',
  },
});
```

### Filesystem Listing

```typescript
const result = await client.query('storage/file_list', {
  params: {
    path: 'reports/2024',
    recursive: true,
    pattern: '*.json',
  },
});

result.rows.forEach(item => {
  console.log(`${item.type.padEnd(10)} ${item.size.toString().padStart(10)} ${item.name}`);
});
```

### Storage Listing Schema

All storage adapters return a consistent JSON schema:

| Column | Type | Description |
|--------|------|-------------|
| `name` | `string` | File or directory name |
| `path` | `string` | Full path within storage |
| `type` | `string` | `"file"` or `"directory"` |
| `size` | `number` | Size in bytes (0 for directories) |
| `modified` | `string \| null` | Last modified time (ISO8601), null for directories |
| `etag` | `string \| null` | Object ETag (S3/MinIO) |
| `storageClass` | `string \| null` | Storage class (S3/MinIO) |
| `contentType` | `string \| null` | MIME type |

### Storage Pagination

```typescript
// Page through large bucket
let result = await client.query('storage/s3_list', {
  params: { bucket: 'my-bucket', prefix: 'logs/' },
  limit: 1000,
});

const allFiles: StorageItem[] = [...result.rows];

while (result.metadata.hasMore) {
  result = await client.query('storage/s3_list', {
    params: { bucket: 'my-bucket', prefix: 'logs/' },
    offset: result.metadata.nextOffset,
    limit: 1000,
  });
  allFiles.push(...result.rows);
}

console.log(`Total files: ${allFiles.length}`);
```

---

## Performance Tips

### 1. Use Time Bounds

```typescript
// GOOD: Limit time range to reduce data scanned
const result = await client.query('analytics/user_activity', {
  timeFrom: '2024-01-01T00:00:00Z',
  timeTo: '2024-01-02T00:00:00Z',
});

// BAD: No time bounds on time-series table
const result = await client.query('analytics/user_activity');
```

### 2. Use Pagination

```typescript
// For large datasets, paginate instead of loading all at once
const pageSize = 100;
let offset = 0;
let allRows: Row[] = [];

while (true) {
  const result = await client.query('analytics/all_events', {
    limit: pageSize,
    offset,
  });

  allRows.push(...result.rows);

  if (result.rows.length < pageSize) break;
  offset += pageSize;
}
```

### 3. Streaming Large Results

```typescript
import { queryStream } from '@hyperi/query-client';

// Stream results in batches
for await (const batch of queryStream('analytics/all_events', {
  batchSize: 10_000,
})) {
  await processBatch(batch.rows);
}
```

### 4. Cache Configuration

```typescript
// Disable caching for real-time data
const result = await client.query('hunts/active_threats', {
  params: { severities: ['critical'] },
  cache: false,  // Always get fresh data
});
```

---

## Bundle Size Optimization

### Tree Shaking

```typescript
// Import only what you need
import { query } from '@hyperi/query-client/core';
import { useQuery } from '@hyperi/query-client/react';

// Avoid default import which includes everything
// import QueryClient from '@hyperi/query-client';
```

---

## References

- [TanStack React Query](https://tanstack.com/query/latest)
- [Query Gateway API Specification](query-api.md)
