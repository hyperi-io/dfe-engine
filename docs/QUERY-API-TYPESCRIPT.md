# DFE Query API - TypeScript SDK

**Version:** 1.0.0
**Last Updated:** 2026-01-16

This document specifies how to consume the DFE Query API from TypeScript/JavaScript applications.

---

## Overview

The Query API returns Apache Arrow IPC streams. This SDK wraps the Arrow deserialization and provides a type-safe, ergonomic interface that integrates with React Query and Effect.

---

## Requirements

### Minimum Versions

| Package | Version | Purpose |
|---------|---------|---------|
| `apache-arrow` | ≥18.0.0 | Arrow IPC deserialization |
| `@tanstack/react-query` | ≥5.0.0 | Data fetching hooks (optional) |
| `@effect/schema` | ≥0.75.0 | Runtime type validation (optional) |
| TypeScript | ≥5.3 | Type definitions |
| Node.js | ≥20.0 | Runtime (for SSR) |

### Installation

```bash
# Core dependency (required)
npm install apache-arrow@^18.0.0

# Optional: React Query integration
npm install @tanstack/react-query@^5.0.0

# Optional: Effect Schema integration
npm install @effect/schema@^0.75.0
```

For minimal bundle size, use the ESModules package:

```bash
npm install @apache-arrow/esnext-esm@^18.0.0
```

---

## Quick Start

### Basic Usage

```typescript
import { query, QueryResult } from '@hypersec/query-client';

// Execute query
const result = await query({
  datasource: 'clickhouse:default',
  query: 'SELECT * FROM logs LIMIT 100',
});

console.log(`Rows: ${result.rowCount}`);
console.log(`Columns: ${result.columns.map(c => c.name)}`);

// Access rows as typed objects
result.rows.forEach(row => {
  console.log(row.timestamp, row.message);
});
```

### With React Query

```typescript
import { useQuery } from '@hypersec/query-client/react';

function LogViewer() {
  const { data, isLoading, error } = useQuery(
    'clickhouse:default',
    'SELECT * FROM logs WHERE level = {level:String} LIMIT 100',
    { level: 'ERROR' }
  );

  if (isLoading) return <Loading />;
  if (error) return <Error error={error} />;

  return (
    <table>
      <thead>
        <tr>
          {data.columns.map(col => <th key={col.name}>{col.name}</th>)}
        </tr>
      </thead>
      <tbody>
        {data.rows.map((row, i) => (
          <tr key={i}>
            {data.columns.map(col => <td key={col.name}>{row[col.name]}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

---

## Installation & Setup

### 1. Create the Query Client Package

Create `packages/dfe-query-client/` in your monorepo:

```
packages/dfe-query-client/
├── package.json
├── tsconfig.json
└── src/
    ├── index.ts
    ├── client.ts
    ├── types.ts
    ├── react.ts        # React Query hooks
    └── effect.ts       # Effect integration
```

### 2. Package Configuration

```json
{
  "name": "@hypersec/query-client",
  "version": "1.0.0",
  "type": "module",
  "exports": {
    ".": "./dist/index.js",
    "./react": "./dist/react.js",
    "./effect": "./dist/effect.js"
  },
  "dependencies": {
    "apache-arrow": "^18.0.0"
  },
  "peerDependencies": {
    "@tanstack/react-query": "^5.0.0",
    "@effect/schema": "^0.75.0"
  },
  "peerDependenciesMeta": {
    "@tanstack/react-query": { "optional": true },
    "@effect/schema": { "optional": true }
  }
}
```

---

## Core Implementation

### Types (`src/types.ts`)

```typescript
/**
 * Column metadata from Arrow schema.
 */
export interface Column {
  name: string;
  type: string;
  nullable: boolean;
}

/**
 * Query execution metadata.
 */
export interface QueryMetadata {
  rowCount: number;
  queryDurationMs: number;
  datasource: string;
  truncated: boolean;
  cached: boolean;
  explainDurationMs?: number;
}

/**
 * Single step in EXPLAIN plan.
 */
export interface ExplainStep {
  stepType: 'read' | 'filter' | 'aggregate' | 'sort' | 'join' | 'projection' | 'limit' | 'union' | 'unknown';
  description: string;
  estimatedRows?: number;
  estimatedCost?: number;
  actualRows?: number;
  actualTimeMs?: number;
  details?: Record<string, unknown>;
}

/**
 * Query execution plan.
 */
export interface ExplainPlan {
  steps: ExplainStep[];
  totalEstimatedCost?: number;
  totalEstimatedRows?: number;
  warnings: string[];
  rawPlan?: string;
}

/**
 * Query result with typed rows.
 */
export interface QueryResult<T = Record<string, unknown>> {
  rows: T[];
  columns: Column[];
  metadata: QueryMetadata;
  explain?: ExplainPlan;
}

/**
 * Query request options.
 */
export interface QueryOptions {
  timeout?: number;
  includeExplain?: boolean;
  parallel?: boolean;
}
```

### Client (`src/client.ts`)

```typescript
import { tableFromIPC, Table } from 'apache-arrow';
import type { Column, QueryMetadata, QueryResult, ExplainPlan, QueryOptions } from './types';

/**
 * Query API base URL. Configure via environment or direct assignment.
 */
let baseUrl = '/api/v1';

export function setBaseUrl(url: string): void {
  baseUrl = url;
}

/**
 * Execute a query against the Query API.
 *
 * @param datasource - Datasource URI (e.g., 'clickhouse:default')
 * @param sql - Query string
 * @param params - Optional query parameters
 * @param options - Query options
 * @returns Query result with typed rows
 */
export async function query<T = Record<string, unknown>>(
  datasource: string,
  sql: string,
  params?: Record<string, unknown>,
  options?: QueryOptions,
): Promise<QueryResult<T>> {
  const response = await fetch(`${baseUrl}/query`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      datasource,
      query: sql,
      params,
      options: {
        timeout_seconds: options?.timeout ?? 30,
        include_explain: options?.includeExplain ?? false,
        parallel: options?.parallel ?? false,
      },
    }),
  });

  if (!response.ok) {
    const error = await response.text();
    throw new QueryError(response.status, error);
  }

  // Parse Arrow IPC response
  const buffer = await response.arrayBuffer();
  const table = tableFromIPC(buffer);

  // Extract metadata from headers
  const metadata: QueryMetadata = {
    rowCount: parseInt(response.headers.get('X-Row-Count') ?? '0', 10),
    queryDurationMs: parseInt(response.headers.get('X-Query-Duration-Ms') ?? '0', 10),
    datasource,
    truncated: response.headers.get('X-Truncated') === 'true',
    cached: response.headers.get('X-Cached') === 'true',
    explainDurationMs: parseInt(response.headers.get('X-Explain-Duration-Ms') ?? '0', 10) || undefined,
  };

  // Extract columns from Arrow schema
  const columns: Column[] = table.schema.fields.map(field => ({
    name: field.name,
    type: String(field.type),
    nullable: field.nullable,
  }));

  // Extract EXPLAIN from Arrow metadata (if present)
  const explain = extractExplainFromMetadata(table);

  return {
    rows: table.toArray() as T[],
    columns,
    metadata,
    explain,
  };
}

/**
 * Query with EXPLAIN plan.
 */
export async function queryWithExplain<T = Record<string, unknown>>(
  datasource: string,
  sql: string,
  params?: Record<string, unknown>,
  parallel = true,
): Promise<QueryResult<T>> {
  return query<T>(datasource, sql, params, {
    includeExplain: true,
    parallel,
  });
}

/**
 * Extract EXPLAIN plan from Arrow schema metadata.
 */
function extractExplainFromMetadata(table: Table): ExplainPlan | undefined {
  const metadata = table.schema.metadata;
  if (!metadata) return undefined;

  const stepsJson = metadata.get('dfe:explain:steps');
  if (!stepsJson) return undefined;

  try {
    const data = JSON.parse(stepsJson);
    const warningsStr = metadata.get('dfe:explain:warnings') ?? '';

    return {
      steps: data.steps ?? [],
      warnings: warningsStr ? warningsStr.split(',') : [],
      rawPlan: metadata.get('dfe:explain:raw') ?? undefined,
      totalEstimatedCost: parseFloat(metadata.get('dfe:explain:estimated_cost') ?? '') || undefined,
    };
  } catch {
    return undefined;
  }
}

/**
 * Query API error.
 */
export class QueryError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: string,
  ) {
    super(`Query failed (${status}): ${body}`);
    this.name = 'QueryError';
  }
}
```

### React Query Hooks (`src/react.ts`)

```typescript
import {
  useQuery as useReactQuery,
  useMutation,
  type UseQueryOptions,
  type UseQueryResult,
} from '@tanstack/react-query';
import { query, queryWithExplain, type QueryResult, type QueryOptions } from './client';

/**
 * React Query hook for executing queries.
 *
 * @example
 * ```tsx
 * const { data, isLoading } = useQuery(
 *   'clickhouse:default',
 *   'SELECT * FROM logs LIMIT 100'
 * );
 * ```
 */
export function useQuery<T = Record<string, unknown>>(
  datasource: string,
  sql: string,
  params?: Record<string, unknown>,
  options?: QueryOptions & {
    enabled?: boolean;
    staleTime?: number;
    refetchInterval?: number;
  },
): UseQueryResult<QueryResult<T>> {
  return useReactQuery({
    queryKey: ['dfe-query', datasource, sql, params],
    queryFn: () => query<T>(datasource, sql, params, options),
    enabled: options?.enabled ?? true,
    staleTime: options?.staleTime ?? 5 * 60 * 1000, // 5 minutes
    refetchInterval: options?.refetchInterval,
  });
}

/**
 * React Query hook for queries with EXPLAIN plan.
 *
 * @example
 * ```tsx
 * const { data } = useQueryWithExplain(
 *   'clickhouse:default',
 *   'SELECT * FROM events WHERE level = {level:String}',
 *   { level: 'ERROR' },
 *   { parallel: true }
 * );
 *
 * // Access EXPLAIN
 * data?.explain?.steps.forEach(step => console.log(step));
 * ```
 */
export function useQueryWithExplain<T = Record<string, unknown>>(
  datasource: string,
  sql: string,
  params?: Record<string, unknown>,
  options?: {
    parallel?: boolean;
    enabled?: boolean;
    staleTime?: number;
  },
): UseQueryResult<QueryResult<T>> {
  return useReactQuery({
    queryKey: ['dfe-query-explain', datasource, sql, params],
    queryFn: () => queryWithExplain<T>(datasource, sql, params, options?.parallel),
    enabled: options?.enabled ?? true,
    staleTime: options?.staleTime ?? 5 * 60 * 1000,
  });
}

/**
 * Mutation hook for ad-hoc queries (e.g., from a query editor).
 *
 * @example
 * ```tsx
 * const mutation = useQueryMutation();
 *
 * const handleSubmit = (sql: string) => {
 *   mutation.mutate({
 *     datasource: 'clickhouse:default',
 *     sql,
 *   });
 * };
 * ```
 */
export function useQueryMutation<T = Record<string, unknown>>() {
  return useMutation({
    mutationFn: ({
      datasource,
      sql,
      params,
      options,
    }: {
      datasource: string;
      sql: string;
      params?: Record<string, unknown>;
      options?: QueryOptions;
    }) => query<T>(datasource, sql, params, options),
  });
}
```

### Effect Integration (`src/effect.ts`)

```typescript
import { Effect, Layer, Context } from 'effect';
import { Schema } from '@effect/schema';
import { query as rawQuery, type QueryResult, type QueryOptions } from './client';

/**
 * Query client service for Effect.
 */
export class QueryClient extends Context.Tag('QueryClient')<
  QueryClient,
  {
    readonly query: <T>(
      datasource: string,
      sql: string,
      params?: Record<string, unknown>,
      options?: QueryOptions,
    ) => Effect.Effect<QueryResult<T>, QueryError>;
  }
>() {}

/**
 * Query error for Effect.
 */
export class QueryError extends Schema.TaggedError<QueryError>()('QueryError', {
  status: Schema.Number,
  message: Schema.String,
}) {}

/**
 * Live implementation of QueryClient.
 */
export const QueryClientLive = Layer.succeed(QueryClient, {
  query: <T>(
    datasource: string,
    sql: string,
    params?: Record<string, unknown>,
    options?: QueryOptions,
  ) =>
    Effect.tryPromise({
      try: () => rawQuery<T>(datasource, sql, params, options),
      catch: (error) =>
        new QueryError({
          status: error instanceof Error && 'status' in error ? (error as any).status : 500,
          message: String(error),
        }),
    }),
});

/**
 * Execute query in Effect context.
 *
 * @example
 * ```typescript
 * const program = Effect.gen(function* () {
 *   const client = yield* QueryClient;
 *   const result = yield* client.query<LogRow>(
 *     'clickhouse:default',
 *     'SELECT * FROM logs'
 *   );
 *   return result.rows;
 * });
 *
 * const rows = await program.pipe(
 *   Effect.provide(QueryClientLive),
 *   Effect.runPromise
 * );
 * ```
 */
```

---

## Usage Examples

### Basic Query

```typescript
import { query } from '@hypersec/query-client';

interface LogRow {
  timestamp: string;
  level: string;
  message: string;
}

const result = await query<LogRow>(
  'clickhouse:default',
  'SELECT timestamp, level, message FROM logs LIMIT 100'
);

result.rows.forEach(row => {
  console.log(`[${row.level}] ${row.timestamp}: ${row.message}`);
});
```

### With Parameters

```typescript
const result = await query<EventRow>(
  'clickhouse:default',
  `SELECT * FROM events
   WHERE org_id = {org:String}
     AND timestamp > {start:DateTime}
   LIMIT {limit:UInt32}`,
  {
    org: 'acme-corp',
    start: '2024-01-01 00:00:00',
    limit: 1000,
  }
);
```

### Query with EXPLAIN

```typescript
import { queryWithExplain } from '@hypersec/query-client';

const result = await queryWithExplain(
  'clickhouse:default',
  'SELECT org_id, count() FROM events GROUP BY org_id ORDER BY count() DESC',
  undefined,
  true // parallel execution
);

console.log(`Query took ${result.metadata.queryDurationMs}ms`);

// Analyze execution plan
result.explain?.steps.forEach(step => {
  console.log(`${step.stepType}: ${step.description}`);
  if (step.estimatedRows) {
    console.log(`  Estimated rows: ${step.estimatedRows}`);
  }
});

// Check for performance warnings
result.explain?.warnings.forEach(warning => {
  console.warn(`⚠️ ${warning}`);
});
```

### React Component

```tsx
import { useQuery } from '@hypersec/query-client/react';

interface MetricRow {
  timestamp: string;
  value: number;
  metric_name: string;
}

function MetricsChart({ metricName }: { metricName: string }) {
  const { data, isLoading, error, refetch } = useQuery<MetricRow>(
    'clickhouse:default',
    `SELECT timestamp, value, metric_name
     FROM metrics
     WHERE metric_name = {name:String}
       AND timestamp > now() - INTERVAL 1 HOUR
     ORDER BY timestamp`,
    { name: metricName },
    { refetchInterval: 30_000 } // Refresh every 30s
  );

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBanner error={error} onRetry={refetch} />;

  return (
    <LineChart
      data={data.rows}
      xKey="timestamp"
      yKey="value"
      title={`${metricName} (${data.metadata.queryDurationMs}ms)`}
    />
  );
}
```

### Query Editor with EXPLAIN

```tsx
import { useQueryMutation, useQueryWithExplain } from '@hypersec/query-client/react';
import { useState } from 'react';

function QueryEditor() {
  const [sql, setSql] = useState('');
  const [showExplain, setShowExplain] = useState(false);
  const mutation = useQueryMutation();

  const handleRun = () => {
    mutation.mutate({
      datasource: 'clickhouse:default',
      sql,
      options: { includeExplain: showExplain, parallel: true },
    });
  };

  return (
    <div>
      <textarea value={sql} onChange={e => setSql(e.target.value)} />

      <label>
        <input
          type="checkbox"
          checked={showExplain}
          onChange={e => setShowExplain(e.target.checked)}
        />
        Show EXPLAIN
      </label>

      <button onClick={handleRun} disabled={mutation.isPending}>
        {mutation.isPending ? 'Running...' : 'Run Query'}
      </button>

      {mutation.data && (
        <>
          <ResultsTable result={mutation.data} />
          {mutation.data.explain && (
            <ExplainPanel explain={mutation.data.explain} />
          )}
        </>
      )}
    </div>
  );
}

function ExplainPanel({ explain }: { explain: ExplainPlan }) {
  return (
    <div className="explain-panel">
      <h3>Execution Plan</h3>
      {explain.warnings.length > 0 && (
        <div className="warnings">
          {explain.warnings.map((w, i) => (
            <div key={i} className="warning">⚠️ {w}</div>
          ))}
        </div>
      )}
      <ol>
        {explain.steps.map((step, i) => (
          <li key={i}>
            <strong>{step.stepType}</strong>: {step.description}
            {step.estimatedRows && (
              <span className="estimate">~{step.estimatedRows} rows</span>
            )}
          </li>
        ))}
      </ol>
      {explain.rawPlan && (
        <details>
          <summary>Raw Plan</summary>
          <pre>{explain.rawPlan}</pre>
        </details>
      )}
    </div>
  );
}
```

---

## Bundle Size Optimization

### Use Specific Module Format

```bash
# Full package (all formats) - ~450KB
npm install apache-arrow

# ESModules only - ~150KB
npm install @apache-arrow/esnext-esm
```

### Tree Shaking

Import only what you need:

```typescript
// GOOD: Named imports (tree-shakeable)
import { tableFromIPC } from 'apache-arrow';

// BAD: Namespace import (includes everything)
import * as Arrow from 'apache-arrow';
```

### Lazy Loading

Load Arrow only when needed:

```typescript
async function query(datasource: string, sql: string) {
  // Arrow loaded on first query
  const { tableFromIPC } = await import('apache-arrow');

  const response = await fetch('/api/v1/query', { ... });
  const buffer = await response.arrayBuffer();
  return tableFromIPC(buffer);
}
```

---

## Error Handling

```typescript
import { query, QueryError } from '@hypersec/query-client';

try {
  const result = await query('clickhouse:default', 'SELECT * FROM logs');
} catch (error) {
  if (error instanceof QueryError) {
    switch (error.status) {
      case 400:
        console.error('Invalid query:', error.body);
        break;
      case 401:
        // Redirect to login
        window.location.href = '/login';
        break;
      case 504:
        console.error('Query timed out');
        break;
      default:
        console.error('Query failed:', error.message);
    }
  } else {
    // Network error
    console.error('Network error:', error);
  }
}
```

---

## References

- [Apache Arrow JavaScript Documentation](https://arrow.apache.org/docs/js/index.html)
- [apache-arrow on npm](https://www.npmjs.com/package/apache-arrow) (v18.0.0+)
- [Arrow.js GitHub Repository](https://github.com/apache/arrow-js)
- [TanStack React Query](https://tanstack.com/query/latest)
