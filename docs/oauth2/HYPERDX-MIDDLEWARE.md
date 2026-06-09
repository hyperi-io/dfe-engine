# HyperDX OIDC Connection Middleware

**Status:** Proposed
**Last Updated:** 2026-02-16

---

## Problem

HyperDX connects to ClickHouse using a single set of credentials per Connection
object. When used as both the observability UI and data exploration UI, different
users need different ClickHouse access levels based on their organizational role.

HyperDX has no native OIDC integration and no concept of mapping an
authenticated user's identity to a specific ClickHouse connection.

### Provider Compatibility Challenge

Different OIDC providers expose group information differently:

| Provider | Groups in Token | Format | Notes |
|----------|----------------|--------|-------|
| **Entra ID** | Yes (with config) | Object IDs (GUIDs) | Can emit `cloud_displayname` via optional claims but requires non-Free tier and is a preview feature |
| **Okta** | Yes | Display names | Most straightforward — configure a groups claim and get names |
| **Google** | No groups claim | N/A | Google Workspace has no standard groups claim; group membership requires the Admin SDK Directory API |

The middleware must handle all three without provider-specific code.

## Solution

A single Express middleware file that resolves the correct HyperDX Connection
per request. It supports three claim sources (checked in order):

1. **Groups** (`X-Forwarded-Groups`) — works with Entra ID (GUIDs) and Okta
   (names)
2. **Email pattern** (`X-Forwarded-Email`) — works with all providers, including
   Google where groups aren't available
3. **Default connection** — fallback when no claim matches

The mapping configuration is provider-agnostic: the env var values are whatever
the provider actually emits (GUIDs for Entra ID, names for Okta, emails for
Google).

## Architecture

```
User (browser)
  │
  ▼
oauth2-proxy (Entra ID / Okta / Google OIDC)
  │  authenticates user
  │  injects X-Forwarded-User, X-Forwarded-Email, X-Forwarded-Groups
  │
  ▼
HyperDX API (Express)
  │
  ├─ isUserAuthenticated          (existing - validates session)
  ├─ resolveOidcConnection        (NEW - maps claims → connection)
  ├─ hasConnectionId              (existing - validates header)
  ├─ getConnection                (existing - loads Connection from MongoDB)
  └─ proxyMiddleware              (existing - proxies to ClickHouse)
  │
  ▼
ClickHouse (with RBAC users/roles/GRANTs)
```

## Claim Matching

### Resolution Order

The middleware checks claims in this order and stops at the first match:

1. **Explicit header** — if `x-hyperdx-connection-id` is already set by the
   client, skip entirely (user selected a connection in the UI)
2. **Group match** — check `X-Forwarded-Groups` against
   `OIDC_GROUP_CONNECTION_MAP`
3. **Email match** — check `X-Forwarded-Email` against
   `OIDC_EMAIL_CONNECTION_MAP`
4. **Default** — use `OIDC_DEFAULT_CONNECTION` if set
5. **Pass through** — no injection, existing HyperDX behaviour

### Group Matching

The `X-Forwarded-Groups` header contains a comma-separated list of whatever the
OIDC provider emits. The middleware iterates through the user's groups and looks
each up in the mapping. **First match wins.**

Configure from most-privileged to least-privileged:

```bash
# Entra ID — groups are GUIDs
OIDC_GROUP_CONNECTION_MAP='{
  "ac51800c-2679-4ecb-8130-636380a3b491": "ch-admin",
  "b7f3a2d1-9e4c-4f8a-b123-456789abcdef": "ch-analyst",
  "c8d4b3e2-0f5d-5a9b-c234-567890abcdef": "ch-readonly"
}'

# Okta — groups are display names
OIDC_GROUP_CONNECTION_MAP='{
  "DFE-Admins": "ch-admin",
  "DFE-Analysts": "ch-analyst",
  "DFE-Viewers": "ch-readonly"
}'
```

### Email Matching

For providers that don't support group claims (Google), or as a supplementary
mapping. Supports exact email and `@domain` suffix matching:

```bash
# Google Workspace — map by email domain or specific user
OIDC_EMAIL_CONNECTION_MAP='{
  "admin@hyperi.io": "ch-admin",
  "@hyperi.io": "ch-analyst",
  "@contractor.example.com": "ch-readonly"
}'
```

Matching rules:
- Exact email match is checked first (`admin@hyperi.io`)
- Domain suffix match is checked second (`@hyperi.io`)
- First match wins within each category

### Default Connection

Fallback when no group or email matches:

```bash
OIDC_DEFAULT_CONNECTION=ch-readonly
```

### Fallback Behaviour

- If no mapping env vars are set: middleware is a complete no-op
- If no claim matches any mapping: pass through (user picks connection manually
  in UI)
- If the client already sent `x-hyperdx-connection-id`: pass through (explicit
  selection takes precedence)

## Prerequisites

### 1. ClickHouse Users and Roles

Create ClickHouse users with appropriate GRANTs:

```sql
-- Read-only analyst (limited databases)
CREATE ROLE analyst_readonly;
GRANT SELECT ON common.* TO analyst_readonly;
CREATE USER ch_readonly IDENTIFIED BY '...' DEFAULT ROLE analyst_readonly;

-- Standard analyst (broader access)
CREATE ROLE analyst_standard;
GRANT SELECT ON common.*, threat_intel.*, soc_events.* TO analyst_standard;
CREATE USER ch_analyst IDENTIFIED BY '...' DEFAULT ROLE analyst_standard;

-- Admin (full read access)
CREATE ROLE analyst_admin;
GRANT SELECT ON *.* TO analyst_admin;
CREATE USER ch_admin IDENTIFIED BY '...' DEFAULT ROLE analyst_admin;
```

### 2. HyperDX Connections

Create Connection objects in HyperDX (via UI or MongoDB seed) that use these
ClickHouse users:

| Connection Name | ClickHouse User | Purpose |
|-----------------|-----------------|---------|
| `ch-readonly`   | `ch_readonly`   | Viewer-tier access |
| `ch-analyst`    | `ch_analyst`    | Analyst-tier access |
| `ch-admin`      | `ch_admin`      | Admin-tier access |

All connections point to the same ClickHouse host — only the credentials differ.

### 3. Identity Provider Configuration

#### Entra ID

1. App Registration > Token Configuration > Add groups claim
2. Select "Security groups" under group types
3. Note: groups will appear as object IDs (GUIDs) in the token
4. Optional: enable `cloud_displayname` in `additionalProperties` for name-based
   matching (requires Entra ID P1+, preview feature)
5. If >200 groups: configure `User.Read` scope so oauth2-proxy can handle
   overage via Graph API

#### Okta

1. Admin > Security > API > Authorization Servers > Claims
2. Add a `groups` claim with filter "Matches regex: `.*`" (or specific prefix)
3. Groups appear as display names by default

#### Google

1. Google Workspace does not emit groups in OIDC tokens
2. Use `OIDC_EMAIL_CONNECTION_MAP` for email-based mapping instead
3. Alternative: configure Google Workspace custom attributes as OIDC claims

### 4. Ingress / Authentication Layer

There are two deployment options. **Envoy Gateway is recommended** — it replaces
both nginx-ingress and oauth2-proxy with a single component.

#### Option A: Envoy Gateway (Recommended)

Envoy Gateway has native OIDC support via `SecurityPolicy`. No oauth2-proxy
needed. See `spike/envoy-gateway.yaml` for the full configuration.

- Handles OIDC login flow, token validation, and claim extraction natively
- Forwards claims as HTTP headers to upstream (HyperDX API)
- Uses Kubernetes Gateway API (`HTTPRoute`, `SecurityPolicy`) — the successor
  to the Ingress API
- Single component replaces both nginx-ingress and oauth2-proxy

> **Note:** The community `kubernetes/ingress-nginx` controller reaches
> [EOL March 2026](https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/).
> No further security patches will be issued. Envoy Gateway is the recommended
> migration path.

#### Option B: oauth2-proxy + nginx (Legacy)

If migrating to Envoy Gateway is not yet feasible, the traditional pattern
works. See `spike/oauth2-proxy.cfg` and `spike/nginx.conf` for configs.

```yaml
# oauth2-proxy — common config (all providers)
set_xauthrequest: true
pass_user_headers: true

# Entra ID
provider: entra-id
oidc_issuer_url: https://login.microsoftonline.com/<tenant-id>/v2.0
scope: "openid email profile"

# Okta
provider: oidc
oidc_issuer_url: https://<org>.okta.com
scope: "openid email profile groups"

# Google
provider: google
scope: "openid email profile"
# No groups scope — use email mapping
```

> **Warning:** The `kubernetes/ingress-nginx` community project is EOL
> March 2026. If you must use nginx, use the F5-maintained
> `nginxinc/nginx-ingress` controller (separate project, actively maintained).

## Implementation Scope

### Files to Create

| File | Lines | Purpose |
|------|-------|---------|
| `packages/api/src/middleware/oidc-connection.ts` | ~80 | Claim-to-connection mapping middleware |

### Files to Modify

| File | Change | Lines Changed |
|------|--------|---------------|
| `packages/api/src/api-app.ts` | Import middleware + add to `/clickhouse-proxy` route chain | 2 |

### No Changes Required

- Models (Connection, User, Team) — used as-is
- Frontend (app) — no UI changes
- ClickHouse proxy logic — unchanged
- Authentication flow — unchanged

## Middleware Implementation

```typescript
// packages/api/src/middleware/oidc-connection.ts
//
// Maps oauth2-proxy identity headers to a HyperDX Connection.
// Supports group claims (Entra ID, Okta) and email (Google, all providers).
// Slots into the existing middleware chain before hasConnectionId.

import type { NextFunction, Request, Response } from 'express';

import Connection from '@/models/connection';
import logger from '@/utils/logger';

// --- Configuration from environment ---

type ClaimMap = Record<string, string>;

function parseEnvMap(envVar: string | undefined): ClaimMap {
  if (!envVar) return {};
  try {
    return JSON.parse(envVar);
  } catch (e) {
    logger.error(`Invalid JSON in ${envVar}: ${e}`);
    return {};
  }
}

const groupMap = parseEnvMap(process.env.OIDC_GROUP_CONNECTION_MAP);
const emailMap = parseEnvMap(process.env.OIDC_EMAIL_CONNECTION_MAP);
const defaultConnection = process.env.OIDC_DEFAULT_CONNECTION ?? '';

const isEnabled =
  Object.keys(groupMap).length > 0 ||
  Object.keys(emailMap).length > 0 ||
  defaultConnection !== '';

// --- Helpers ---

function getHeader(req: Request, name: string): string | undefined {
  const val = req.headers[name];
  if (!val) return undefined;
  return Array.isArray(val) ? val[0] : val;
}

function matchGroup(groups: string[]): string | undefined {
  for (const group of groups) {
    const connectionName = groupMap[group];
    if (connectionName) return connectionName;
  }
  return undefined;
}

function matchEmail(email: string): string | undefined {
  // Exact match first
  if (emailMap[email]) return emailMap[email];

  // Domain suffix match
  const atIndex = email.indexOf('@');
  if (atIndex !== -1) {
    const domain = email.substring(atIndex); // "@example.com"
    if (emailMap[domain]) return emailMap[domain];
  }

  return undefined;
}

// --- Middleware ---

export async function resolveOidcConnection(
  req: Request,
  _res: Response,
  next: NextFunction,
) {
  // Don't override explicit connection selection
  if (req.headers['x-hyperdx-connection-id']) {
    return next();
  }

  // No mapping configured — skip entirely
  if (!isEnabled) {
    return next();
  }

  const teamId = req.user?.team;
  if (!teamId) {
    return next();
  }

  let connectionName: string | undefined;

  // 1. Try group match (Entra ID GUIDs, Okta names)
  const groupsHeader = getHeader(req, 'x-forwarded-groups');
  if (groupsHeader && Object.keys(groupMap).length > 0) {
    const groups = groupsHeader.split(',').map(g => g.trim());
    connectionName = matchGroup(groups);
  }

  // 2. Try email match (Google, fallback for all providers)
  if (!connectionName) {
    const email = getHeader(req, 'x-forwarded-email');
    if (email && Object.keys(emailMap).length > 0) {
      connectionName = matchEmail(email);
    }
  }

  // 3. Try default connection
  if (!connectionName && defaultConnection) {
    connectionName = defaultConnection;
  }

  // No match — pass through to existing behaviour
  if (!connectionName) {
    return next();
  }

  // Resolve connection name to MongoDB ObjectId
  const connection = await Connection.findOne({
    name: connectionName,
    team: teamId,
  });

  if (connection) {
    req.headers['x-hyperdx-connection-id'] = connection._id.toString();
    logger.debug(`OIDC claim resolved to connection "${connectionName}"`);
  } else {
    logger.warn(
      `OIDC mapped to connection "${connectionName}" but not found in team`,
    );
  }

  next();
}
```

### Route Chain Change

```diff
// packages/api/src/api-app.ts
+ import { resolveOidcConnection } from './middleware/oidc-connection';

- app.use('/clickhouse-proxy', isUserAuthenticated, clickhouseProxyRouter);
+ app.use('/clickhouse-proxy', isUserAuthenticated, resolveOidcConnection, clickhouseProxyRouter);
```

## Configuration

Environment variables on the HyperDX API container:

```yaml
# Group-based mapping (Entra ID or Okta)
# Keys: whatever the provider emits (GUIDs for Entra, names for Okta)
# Values: HyperDX Connection name
OIDC_GROUP_CONNECTION_MAP: '{"ac51800c-...":"ch-admin","b7f3a2d1-...":"ch-analyst"}'

# Email-based mapping (Google, or supplementary for any provider)
# Keys: exact email or @domain suffix
# Values: HyperDX Connection name
OIDC_EMAIL_CONNECTION_MAP: '{"admin@hyperi.io":"ch-admin","@hyperi.io":"ch-analyst"}'

# Default connection when no claim matches (optional)
OIDC_DEFAULT_CONNECTION: 'ch-readonly'
```

**Typical deployments use one or two of these, not all three:**

| Provider | Recommended Config |
|----------|-------------------|
| Entra ID | `OIDC_GROUP_CONNECTION_MAP` (GUIDs) + `OIDC_DEFAULT_CONNECTION` |
| Okta | `OIDC_GROUP_CONNECTION_MAP` (names) + `OIDC_DEFAULT_CONNECTION` |
| Google | `OIDC_EMAIL_CONNECTION_MAP` + `OIDC_DEFAULT_CONNECTION` |

## Deployment Profiles

### Local Development (Docker)

Local dev runs in **god mode** — no authentication, no Envoy, no oauth2-proxy.
The ClickHouse `default` user has full access to all databases. The OIDC
middleware is disabled (no `OIDC_*` env vars set).

HyperDX's `IS_LOCAL_APP_MODE=true` skips authentication entirely. A single
Connection using the ClickHouse `default` user provides unrestricted access.

```yaml
# docker-compose.yml (local dev)
services:
  clickhouse:
    image: clickhouse/clickhouse-server:latest
    ports:
      - "8123:8123"
      - "9000:9000"

  mongodb:
    image: mongo:7
    ports:
      - "27017:27017"

  otel-collector:
    image: docker.hyperdx.io/hyperdx/hyperdx-otel-collector:latest
    ports:
      - "4317:4317"   # OTLP gRPC
      - "4318:4318"   # OTLP HTTP
    environment:
      - CLICKHOUSE_ENDPOINT=http://clickhouse:8123
      - CLICKHOUSE_USER=default
      - CLICKHOUSE_PASSWORD=

  hyperdx:
    image: docker.hyperdx.io/hyperdx/hyperdx-api:latest
    ports:
      - "8080:8080"
      - "8000:8000"
    environment:
      - IS_LOCAL_APP_MODE=true
      - CLICKHOUSE_HOST=http://clickhouse:8123
      - CLICKHOUSE_USER=default
      - CLICKHOUSE_PASSWORD=
      - MONGODB_URI=mongodb://mongodb:27017/hyperdx
      # No OIDC_* vars — middleware is a no-op
      # No auth — god mode, full access to everything
```

No Envoy Gateway, no oauth2-proxy, no TLS. Just the core stack.

### Production (Kubernetes)

Production uses Envoy Gateway with native OIDC and the oidc-connection
middleware for ClickHouse RBAC. See `spike/envoy-gateway.yaml` for the full
configuration.

```
Envoy Gateway (OIDC + TLS) → HyperDX API (oidc-connection middleware) → ClickHouse (RBAC users)
```

- Envoy Gateway handles authentication (no oauth2-proxy needed)
- OIDC claims forwarded as `X-Forwarded-Groups` / `X-Forwarded-Email` headers
- Middleware maps claims to ClickHouse connections with appropriate GRANTs
- OTLP ingestion on ClusterIP service (internal, no auth)

## Maintenance Considerations

- **Upgrade safe:** The middleware only touches the Express route chain at one
  mount point. HyperDX internal logic (Connection model, proxy middleware,
  authentication) is untouched.
- **Feature-flagged by env var:** If no `OIDC_*` env vars are set, the
  middleware is a no-op. Removing the env vars disables the feature.
- **Provider-agnostic:** No provider-specific code. The middleware operates on
  standard HTTP headers that oauth2-proxy emits regardless of the upstream IdP.
- **Forward compatible:** If HyperDX adds native OIDC support in the future,
  this middleware can be removed with no side effects.
- **Merge conflicts:** Limited to `api-app.ts` route registration (one line).
  The middleware file itself is standalone.
- **No new dependencies:** Uses only Express types and the existing Connection
  Mongoose model.

## Testing

### Manual Verification

1. Deploy HyperDX with oauth2-proxy in front
2. Set mapping env vars for your provider
3. Log in as user in admin group → verify queries use `ch-admin` credentials
   (check ClickHouse `system.query_log` for `user` column)
4. Log in as user in viewer group → verify queries use `ch-readonly`
5. Log in as user with no matching group → verify fallback behaviour

### Automated Test

```typescript
// packages/api/src/middleware/__tests__/oidc-connection.test.ts
describe('resolveOidcConnection', () => {
  // Group matching
  it('maps X-Forwarded-Groups GUID to connection (Entra ID)');
  it('maps X-Forwarded-Groups name to connection (Okta)');
  it('uses first matching group (priority order)');

  // Email matching
  it('maps exact email to connection');
  it('maps @domain suffix to connection');
  it('prefers exact email over domain suffix');

  // Resolution order
  it('prefers group match over email match');
  it('falls back to email when no group matches');
  it('falls back to default when no group or email matches');

  // Pass-through
  it('skips when x-hyperdx-connection-id already set');
  it('skips when no headers present');
  it('skips when no mapping configured');
  it('falls through when connection name not found in team');
});
```

## Sequence Diagram

```
User                oauth2-proxy       HyperDX API              MongoDB         ClickHouse
 │                      │                  │                       │                │
 │── GET /search ──────>│                  │                       │                │
 │                      │── authenticate ──>│                      │                │
 │                      │   (IdP)          │                       │                │
 │                      │<── token ────────│                       │                │
 │                      │                  │                       │                │
 │                      │── proxy request ─>│                      │                │
 │                      │   Headers:       │                       │                │
 │                      │   X-Forwarded-Groups: <guid1>,<guid2>   │                │
 │                      │   X-Forwarded-Email: user@hyperi.io   │                │
 │                      │                  │                       │                │
 │                      │                  │── resolveOidcConnection               │
 │                      │                  │   1. check groups → match on <guid1>  │
 │                      │                  │   2. resolve "ch-analyst"             │
 │                      │                  │── findOne({name:"ch-analyst"}) ──────>│
 │                      │                  │<── Connection._id ───────────────────│
 │                      │                  │   set x-hyperdx-connection-id        │
 │                      │                  │                       │                │
 │                      │                  │── getConnection ──────>│               │
 │                      │                  │<── host, user, pass ──│                │
 │                      │                  │                       │                │
 │                      │                  │── proxy to CH ─────────────────────────>│
 │                      │                  │   X-ClickHouse-User: ch_analyst        │
 │                      │                  │<── query results ─────────────────────│
 │<── response ────────────────────────────│                       │                │
```
