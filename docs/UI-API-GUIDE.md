# DFE Engine API — UI Integration Guide

This document covers everything a frontend developer needs to integrate
with the DFE Engine REST API.

## Quick Start

```bash
# Generate TypeScript types from OpenAPI spec
npx openapi-typescript openapi-spec/openapi.json -o src/types/api.d.ts

# Start Prism mock server for development
docker compose -f docker-compose.dev.yaml up prism

# API base URL
# Dev (Prism mock):  http://localhost:4010
# Local engine:      http://localhost:8000
# Production:        Proxied via Envoy Gateway
```

## Authentication

All `/api/v1/*` endpoints require authentication. Three methods supported:

| Method | Header | Use Case |
|--------|--------|----------|
| JWT Bearer | `Authorization: Bearer <token>` | UI login flow |
| API Key | `X-API-Key: dfe_xxxx...` | Machine-to-machine |
| OIDC | `X-Oidc-Subject` (set by Envoy) | Production SSO |

### Login Flow

```typescript
// POST /api/v1/auth/login
const resp = await fetch('/api/v1/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  body: new URLSearchParams({ username, password }),
});
const { access_token } = await resp.json();
// Use: { Authorization: `Bearer ${access_token}` }

// GET /api/v1/auth/me — returns current user info + roles
// POST /api/v1/auth/refresh — refresh token
```

## Error Handling

All errors return a consistent shape:

```typescript
interface ErrorResponse {
  code: string;        // Machine-readable: "not_found", "conflict", "unauthorized"
  message: string;     // Human-readable summary
  errors?: FieldError[];  // Field-level validation details (422 only)
  context?: Record<string, unknown>;  // Structured data for specific errors
}

interface FieldError {
  field: string;    // Dotted path: "schema_config.engine"
  message: string;  // "field required"
  code: string;     // "missing"
}
```

| Status | Code | When |
|--------|------|------|
| 401 | `unauthorized` | Missing/invalid credentials |
| 403 | `forbidden` | Insufficient permissions |
| 404 | `not_found` | Resource doesn't exist |
| 409 | `conflict` | Duplicate name or match conflict |
| 422 | `validation_error` | Request body validation failed |
| 500 | `internal_error` | Unexpected server error |
| 503 | `not_configured` | Required backend not available |

## Pagination

Most list endpoints return `PaginatedResponse<T>`. Some also include a full
collection view in `objects` (see below) so the UI can render a tree or other
aggregate without paginating that structure separately.

```typescript
interface PaginatedResponse<T> {
  items: T[];
  total: number;       // Total across all pages
  page: number;        // Current page (1-based)
  per_page: number;    // -1 means all items in one response (page forced to 1)
  total_pages: number;
  next_page: number | null;   // null = last page
  prev_page: number | null;
}

// List + full side payload (e.g. GET /api/v1/schemas)
interface PaginatedResponseWithObjects<T, ObjectT> extends PaginatedResponse<T> {
  objects: ObjectT;    // All matching rows after filters — not limited to `items`
}

// Nested folder tree used in `objects` for schema list
interface PathTree<T> {
  items: T[];                          // Entries at this path level
  children: Record<string, PathTree<T>>;  // Keyed by path segment
}
```

`items` is the current page only. When `objects` is present, it is built from
the same filtered, sorted result set as `items`, but includes every match
(e.g. a path tree for the schema browser). Re-fetching page 2 still returns the
full `objects` tree; only `items` changes.

```typescript
// TanStack Query — simple list (sources, tasks, …)
const { data, fetchNextPage } = useInfiniteQuery({
  queryKey: ['sources'],
  queryFn: ({ pageParam = 1 }) =>
    fetch(`/api/v1/sources?page=${pageParam}&per_page=25`).then(r => r.json()),
  getNextPageParam: (lastPage) => lastPage.next_page ?? undefined,
});

// Schema list: table from `items`, sidebar tree from `objects` (any page)
type SchemaList = PaginatedResponseWithObjects<SchemaSummaryObject, PathTree<SchemaSummaryObject>>;
const list = await fetch('/api/v1/schemas?page=1&per_page=25').then(r => r.json()) as SchemaList;
const tableRows = list.items;
const pathTree = list.objects;
```

Query parameters: `page` (1-based), `per_page` (1–100 or `-1` for all, default 25),
`search` (text filter), `sort_by`, `sort_order` (asc/desc). Schema list also
accepts repeated `schema_type` (top-level path segment, e.g. `meta`).

## API Surface (74 Endpoints)

### Auth & RBAC (`/api/v1/auth/*`)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/auth/login` | Login, returns JWT |
| POST | `/auth/refresh` | Refresh JWT |
| GET | `/auth/me` | Current user info |
| GET | `/auth/permissions` | Available permissions for current user |
| CRUD | `/auth/accounts` | Account management |
| CRUD | `/auth/groups` | Group + role management |
| CRUD | `/auth/roles` | Role definitions (`config/rbac/roles.yaml`) |
| GET | `/auth/roles/scopes` | Casbin permission scopes for role editors |
| CRUD | `/auth/api-keys` | API key management |
| CRUD | `/auth/oidc-providers` | OIDC provider configuration |
| POST | `/auth/oidc-providers/{name}/sync` | Force group sync |
| GET | `/auth/oidc-providers/{name}/test` | Test provider connectivity |

### Sources (`/api/v1/sources`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/sources` | List sources (paginated) |
| POST | `/sources` | Create source |
| GET | `/sources/{name}` | Get source details |
| GET | `/sources/{name}/versions/{version}` | Get one version snapshot |
| GET | `/sources/{name}/columns` | Composed schema columns for a source version (paginated `items`; `?version=` source id, default `deployed_version`; `?page=` / `?per_page=`, use `-1` for all) |
| POST | `/sources/{name}/build` | Build DDL from a source version (`?version=` source id; default `deployed_version`) |
| POST | `/sources/{name}/plan` | Dry-run deploy plan (`?version=` defaults to `current`; not persisted) |
| POST | `/sources/{name}/deploy` | Apply plan DDL to ClickHouse and set `deployed_version` (`?version=` defaults to `current`) |

Artifact files: `source-builds/{name}.yaml` and `source-deploys/{name}.yaml` (per-version maps like `sources/{name}.yaml`). Plans are not persisted.
| PUT | `/sources/{name}` | Update source |
| DELETE | `/sources/{name}` | Delete source |
| POST | `/sources/bulk` | Bulk import |
| POST | `/sources/seed` | Seed defaults |

### Services (`/api/v1/services`)

Schema-less service config for Rust services (receiver, loader, archiver, etc.)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/services` | List service configs |
| GET/PUT/DELETE | `/services/{service}/{instance}` | CRUD per service+instance |
| POST | `/services/{service}/{instance}/validate` | Validate config |
| GET | `/services/{service}/{instance}/history` | Config change history |
| POST | `/services/seed` | Seed defaults |

### Deployments (`/api/v1/deployments`)

K8s/KEDA deployment config (replicas, resources, scaling).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/deployments` | List deployment configs |
| GET/PUT/DELETE | `/deployments/{service}/{instance}` | CRUD per service+instance |
| POST | `/deployments/{service}/{instance}/size/{size}` | Apply t-shirt size |
| POST | `/deployments/{service}/{instance}/validate` | Validate config |
| POST | `/deployments/seed` | Seed defaults |

### Hunts (`/api/v1/hunts`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/hunts/status` | Engine scheduler status |
| GET | `/hunts` | List configured hunts |
| POST | `/hunts/{name}/run` | Trigger ad-hoc execution (202 + task_id) |

### Queries (`/api/v1/queries`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/queries/views` | List parameterized views |
| GET | `/queries/views/namespaces` | List view namespaces |
| GET | `/queries/views/{label}` | View definition + parameters |
| POST | `/queries/views/{label}/execute` | Execute view → JSON |
| POST | `/queries/raw` | Raw query → JSON |
| GET | `/queries/health` | Query engine health |

View execution returns **JSON** (`QueryResponse`) with fields:
`rows`, `columns`, `row_count`, `query_duration_ms`, `has_more`,
`next_offset`, `request_id`.

### Discovery (`/api/v1/discovery`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/discovery/databases` | List ClickHouse databases |
| GET | `/discovery/tables` | List tables (filter by database, engine) |
| GET | `/discovery/tables/{name}/columns` | Table column metadata |

### Sigma (`/api/v1/sigma`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/sigma/mappings/{source}` | Sigma field mappings for a source |
| POST | `/sigma/views/{source}` | Generate Sigma view DDL |
| POST | `/sigma/views` | Generate all Sigma views |
| GET | `/sigma/logsource` | Find sources for a logsource selector |

### Schemas (`/api/v1/schemas`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/schemas` | List meta schemas (paginated `items` + path `objects` tree) |

### Pipeline (`/api/v1/pipeline`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/pipeline/templates` | List Vector templates |
| POST | `/pipeline/build` | Build pipeline manifests (202 + task_id) |

### Tasks (`/api/v1/tasks`)

Background task polling for long-running operations (hunt execution,
pipeline builds).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/tasks` | List tasks (filter by `kind`) |
| GET | `/tasks/{id}` | Get task status |
| POST | `/tasks/{id}/cancel` | Cancel running task |
| GET | `/tasks/{id}/stream` | SSE progress stream |

#### SSE Stream Events

```typescript
// EventSource: GET /api/v1/tasks/{id}/stream
const es = new EventSource(`/api/v1/tasks/${taskId}/stream`);
es.addEventListener('progress', (e) => {
  const { status, progress, message } = JSON.parse(e.data);
  // Update progress bar
});
es.addEventListener('complete', (e) => {
  const { status, result, error } = JSON.parse(e.data);
  // Task finished — close stream
  es.close();
});
```

### Other

| Method | Path | Purpose |
|--------|------|---------|
| CRUD | `/field-maps` | Field map management |
| CRUD | `/alerts/destinations` | Alert destination management |
| CRUD | `/orgs` | Organisation management |
| POST | `/rules` | Create hunt rule |
| POST | `/rules/validate` | Validate rule |
| GET | `/service-surfaces` | Schema-less service discovery |
| POST | `/transforms/compile` | Compile VRL/CEL transform |
| POST | `/transforms/test` | Test transform against sample data |
| GET | `/system/version` | API version |
| GET | `/system/settings` | Current settings |

## Repository (UI preferences + small objects)

A scope-aligned small-object store for the UI: preferences (light/dark
mode), JSON docs, and small files. Scopes: `system` / `org` / `group` /
`user`. Backed by an engine-only ClickHouse table - never queried directly
by the UI.

### Preferences (the fast path)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/repository/preferences` | Effective merged preferences for the caller |
| PATCH | `/repository/preferences` | JSON merge-patch applied to the caller's USER layer |

GET returns the deep merge of the caller's layers - system, then each of
their orgs, then each of their groups, then the user layer (user wins per
key) - plus the user-layer etag:

```json
{ "preferences": { "theme": "dark", "brand": "hyperi" }, "etag": "2026-07-03T12:00:00.000+00:00" }
```

PATCH takes an RFC 7396 merge patch, writes ONLY the user layer, and
returns the same shape with the new effective doc. `null` deletes a
user-layer key so the value falls back to the inherited layer. The theme
toggle is one call: `PATCH {"theme": "dark"}`.

```typescript
// One GET on boot, PATCH merge-patches after
const prefsQuery = useQuery({
  queryKey: ['preferences'],
  queryFn: () => fetch('/api/v1/repository/preferences').then(r => r.json()),
});

const patchPrefs = useMutation({
  mutationFn: (patch: object) =>
    fetch('/api/v1/repository/preferences', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then(r => r.json()),
  // Response IS the new effective doc - no refetch needed
  onSuccess: (data) => queryClient.setQueryData(['preferences'], data),
});

patchPrefs.mutate({ theme: 'dark' });        // set
patchPrefs.mutate({ theme: null });          // clear -> inherit org/system value
```

Admin/settings UIs write the system/org/group preference layers through
the object endpoints below (namespace `preferences`, key `default`).

### Objects (generic)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/repository/objects/{scope}/{scope_id}/{namespace}` | List metadata (key, content_type, size, updated_by, updated_at, etag) |
| GET | `/repository/objects/{scope}/{scope_id}/{namespace}/{key}` | Raw bytes with stored `Content-Type` + `ETag` headers |
| PUT | `/repository/objects/{scope}/{scope_id}/{namespace}/{key}` | Store raw body (`Content-Type` header is kept; default `application/octet-stream`) |
| DELETE | `/repository/objects/{scope}/{scope_id}/{namespace}/{key}` | Delete (204; 404 if absent) |

Path rules: `scope` is one of `system|org|group|user` (422 otherwise).
For `system` the `scope_id` MUST be `-`. `namespace` and `key` match
`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$` (422 otherwise).

Access: any authenticated user reads the system layer; members read their
own org/group layers and their own user layer; everything else needs the
`repository:read` / `repository:write` RBAC actions at the matching scope
(admins system-wide, org-wildcard admins within their org). Reads you
cannot see return 404 (existence stays hidden); forbidden writes return 403.

### ETag semantics

Every stored object carries an opaque etag (returned in the `ETag`
header and in metadata). Send `If-Match: <etag>` on PUT/PATCH for
optimistic concurrency - a mismatch returns
`412 {"code": "precondition_failed"}` with the current etag in the
`ETag` response header. Omit `If-Match` for last-write-wins.

### Size caps

| What | Limit | Over-limit response |
|------|-------|---------------------|
| Preferences doc (PATCH) | 256 KiB (`DFE_REPOSITORY_MAX_PREFS_BYTES`) | `413 {"code": "payload_too_large"}` |
| Object value (PUT) | 1 MiB (`DFE_REPOSITORY_MAX_OBJECT_BYTES`) | `413 {"code": "payload_too_large"}` |

## TypeScript Type Generation

```bash
# Install openapi-typescript
pnpm add -D openapi-typescript

# Generate types (run after any API change)
npx openapi-typescript ../dfe-engine/openapi-spec/openapi.json \
  -o src/types/dfe-engine-api.d.ts
```

Then use with fetch or a typed client:

```typescript
import type { paths } from './types/dfe-engine-api';

type Source = paths['/api/v1/sources/{name}']['get']['responses']['200']['content']['application/json'];
```

## Mock Server (Prism)

```yaml
# docker-compose.dev.yaml
services:
  prism:
    image: stoplight/prism:4
    command: mock -h 0.0.0.0 /spec/openapi.json
    volumes:
      - ./openapi-spec:/spec
    ports:
      - "4010:4010"
```

Prism generates realistic mock responses from the OpenAPI spec.
Point the UI at `http://localhost:4010` during development.

## Contract Drift Detection

CI validates the committed spec matches the running app:

```bash
uv run python openapi-spec/generate.py
git diff --exit-code openapi-spec/openapi.json
```

If this fails, the spec needs regenerating after route/model changes.
