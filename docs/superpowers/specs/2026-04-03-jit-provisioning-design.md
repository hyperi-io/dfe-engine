# JIT User Provisioning + Org-Scoped CH Isolation + HyperDX Team Binding

**Date:** 3 April 2026
**Status:** Design approved
**Scope:** Org lifecycle with CH provisioning, database-level row policies,
HyperDX team binding, JIT shadow accounts on first OIDC login

---

## Problem

When an external user (OIDC/AD) authenticates for the first time, dfe-engine
has no record of them. There is no HyperDX user provisioning, no per-org
ClickHouse isolation, and no way for the UI to show a unified user list.

Org creation is YAML-only — it does not provision ClickHouse connections,
row policies, or HyperDX teams. These are all manual steps today.

## Solution

Three connected subsystems:

1. **Org lifecycle** — org CRUD triggers CH provisioning, HyperDX team
   creation, and optional dedicated database setup
2. **JIT user provisioning** — first OIDC login creates a shadow account
   with group membership and HyperDX team assignment
3. **Dedicated database toggle** — per-org flag that routes data to a
   dedicated CH database

---

## 1. Org Model Changes

The Org model gains one user-visible field:

```yaml
# config/orgs/acme.yaml
display_name: "ACME Corp"
org_ids: ["acme"]
enabled: true
dedicated_database: false          # NEW — UI toggle, default false
```

Internal metadata (managed by engine, not exposed to UI):

```yaml
database_name: "dfe_acme"          # Derived from org name (dedicated DB only)
hyperdx_team_id: "uuid-here"       # Set after team creation
ch_password_env: "DFE_CH_ORG_ACME_PASSWORD"  # Env var name for CH password
```

### Org Name Validation

Org names become ClickHouse identifiers (`dfe_{name}` database, config file
names). Allowed: `[a-z0-9_]`, max 50 characters. Validated at the API layer
on creation.

---

## 2. ClickHouse Multi-Tenancy Strategy

### Two Approaches, One Per Scenario

dfe-engine uses TWO multi-tenancy strategies depending on the org's config:

| Scenario | Strategy | CH Users | How |
|----------|----------|----------|-----|
| **Shared DB** (default) | Custom settings pattern | Fixed set (3-5) | `getSetting('current_tenant_id')` in row policies |
| **Dedicated DB** | Per-org user | One per org | Full access to `dfe_{org}` database |

**Shared DB (existing — no change):** Uses the `ConnectionRegistry` and
`TenantScopedClient` from the connections module. A small fixed set of CH
users (`dfe_admin`, `dfe_analyst`, `dfe_reader`) each set
`current_tenant_id` per query. Row policies use
`getSetting('current_tenant_id')` — O(1) users regardless of org count.
This is already implemented.

**Dedicated DB (new):** When an org enables `dedicated_database: true`,
dfe-engine provisions:

```sql
-- Dedicated database
CREATE DATABASE IF NOT EXISTS dfe_acme;
-- Deploy table/view schema (same as shared dfe DB)

-- Per-org user with full access to their DB
CREATE USER IF NOT EXISTS dfe_org_acme
    IDENTIFIED WITH sha256_hash BY '...';
GRANT SELECT ON dfe_acme.* TO dfe_org_acme;
```

No row policy needed — the entire database belongs to one org.
The CH user password is stored in a K8s Secret or env var (referenced
by `ch_password_env` in the org metadata), never in YAML.

### Toggling Dedicated Database

**Enabling (`dedicated_database: true`):**
1. Create `dfe_acme` database + deploy schema
2. Create `dfe_org_acme` CH user with access to `dfe_acme.*`
3. Update dfe-loader config git to route `org_id=acme` → `dfe_acme.*`
4. Update HyperDX team connection to point at `dfe_acme`

**Disabling (`dedicated_database: false`):**
Requires `confirm_merge: true` in the API request. Without it, returns 400:

```json
{
  "code": "confirmation_required",
  "message": "Disabling dedicated database will route data back to shared DB. Set confirm_merge=true to proceed. Existing database will NOT be dropped."
}
```

When confirmed:
- Loader routing reverts to shared `dfe` DB
- `dfe_acme` database is NOT dropped (manual admin action)
- CH user is NOT dropped (re-enabling later reuses it)
- HyperDX team connection updated to shared DB + tenant-scoped user
- Re-enabling later re-routes back — data is still there

### Org Deletion

- If dedicated DB: drop CH user, revoke grants. Database NOT dropped.
- HyperDX team deleted (requires adding `delete_team()` to HyperDXClient)
- Shared DB: no CH changes (custom settings pattern, no per-org objects)

---

## 3. HyperDX Team Binding

Org creation provisions a HyperDX team with the appropriate CH connection.

### Team Hierarchy

| Team | CH Connection | Who Gets Assigned | Sees |
|------|--------------|-------------------|------|
| `dfe-admin` | `dfe_admin` (unrestricted) | Users with `admin` role | Everything |
| `dfe-analysts` | `dfe_analyst` (unrestricted) | Users with `data_analyst` role | Everything |
| `customer-{org}` | Tenant-scoped or dedicated user | Org-scoped users only | Only their org's data |

### Connection Strategy Per Team

| Org Config | HyperDX Team Connection |
|-----------|------------------------|
| Shared DB (default) | Uses `dfe_reader` user with `current_tenant_id` set to org_id |
| Dedicated DB | Uses `dfe_org_{name}` user connected to `dfe_{name}` database |

### Broadest-Wins Rule

A user in both an org-scoped group AND an admin/analyst group gets assigned
to the broader team. Precedence order (highest first):
`admin` > `infra_admin` > `data_analyst` > `data_analyst_viewer` >
`data_viewer` > `infra_viewer` > `customer_viewer` (org-scoped).

The org-scoped team is for users who have ONLY `customer_viewer` access
(Kibana-like HyperDX access, nothing else in dfe-engine).

### Provisioning Flow

```
Org "acme" created
    ↓
1. CH provisioning (dedicated DB only — Section 2)
    ↓
2. HyperDXClient.create_team("customer-acme") → team_id
    ↓
3. HyperDXClient.create_connection(team_id, ...)
    → connection depends on shared vs dedicated (see table above)
    ↓
4. Store team_id on Org internal metadata
```

All HyperDX operations are non-fatal — failure logs a warning but does not
block org creation.

---

## 4. JIT User Provisioning (First Login)

When an OIDC user authenticates for the first time, dfe-engine creates a
shadow account and determines HyperDX team assignment.

### Trigger

Inside `get_current_user()`, after successful OIDC auth (Path 1):

```
X-Oidc-Subject: jane@corp.com
X-Oidc-Groups: acme-security, soc-team
    ↓
GroupStore resolves roles + org_ids
    ↓
AccountStore.get("jane-corp-com") → None (first login)
    ↓
JitProvisioner.ensure_account():
    1. Create shadow account (no password, external=true)
    2. Determine HyperDX team (broadest-wins)
    3. Set last_login_at
    ↓
Subsequent logins:
    - Account exists → update groups if changed, update last_login_at
    - No re-provisioning unless group membership changed
```

### Group → Org Mapping

The `Group` model gains an `org_ids` field:

```yaml
# config/auth/groups/acme-viewers.yaml
description: "ACME org viewers"
roles: ["customer_viewer"]
members: []
org_ids: ["acme"]                  # NEW — maps this group to org(s)
source_provider: "corporate-entra"
source_id: "group-uuid"
```

This is how JIT provisioning determines which org a user belongs to and
which HyperDX team to assign. If a user's groups resolve to multiple orgs,
they get the broadest team they qualify for.

### Shadow Account Model

```yaml
# config/auth/accounts/jane-corp-com.yaml
username: "jane@corp.com"
password_hash: ""                    # Empty — cannot local-login
enabled: true
groups: ["acme-security", "soc-team"]
source_provider: "corporate-entra"
external: true                       # Managed by IdP, not local admin
last_login_at: "2026-04-03T10:30:00Z"
```

Username is sanitised for filename: `jane@corp.com` → `jane-corp-com.yaml`.
The `username` field inside the YAML stores the original email.

### Write-on-Auth Handling

The AccountStore read on every OIDC request is a single file lookup —
negligible overhead. Writes (first login or group change) are handled
carefully:

- **First login race:** Two concurrent first requests both try to create
  the file. `AccountStore.create()` raises `ValueError` if it exists.
  `JitProvisioner` catches this and falls through to the update path.
- **HyperDX provisioning:** Fire-and-forget via `asyncio.create_task()`
  — does not block the auth response.

### What JIT Does NOT Do

- Does not create local passwords (external users cannot local-login)
- Does not override admin-managed group assignments (extra groups preserved)
- Does not block login if HyperDX provisioning fails (non-fatal)
- Does not provision on API key or JWT auth paths (OIDC only)

---

## 5. API Surface

### Org Endpoints (Expanded)

| Method | Path | Change |
|--------|------|--------|
| `POST` | `/orgs` | Triggers HyperDX team creation. CH provisioning only if `dedicated_database: true` |
| `PUT` | `/orgs/{name}` | `dedicated_database` toggle (requires `confirm_merge` to disable) |
| `DELETE` | `/orgs/{name}` | Deletes HyperDX team. Drops CH user if dedicated DB. Does NOT drop database |

Org request gains `dedicated_database: bool` (default `false`).
`confirm_merge` is a request-only field (not persisted on the Org model).

### Account Endpoints (Minor Change)

`GET /auth/accounts` and `GET /auth/accounts/{username}` now return shadow
accounts alongside local accounts. New response fields:

- `external: bool` — true for OIDC-provisioned accounts
- `source_provider: string` — which OIDC provider created this
- `last_login_at: string` — updated on each auth

No new provisioning endpoints. JIT is automatic.

---

## 6. Components

### New

| Component | Location | Purpose |
|-----------|----------|---------|
| `OrgChProvisioner` | `src/dfe_engine/orgs/ch_provisioner.py` | CH user + database CRUD (dedicated DB only) |
| `OrgLifecycleManager` | `src/dfe_engine/orgs/lifecycle.py` | Orchestrates CH + HyperDX + loader config |
| `JitProvisioner` | `src/dfe_engine/auth/jit.py` | Shadow account creation, HyperDX team assignment |

### Modified

| Component | Change |
|-----------|--------|
| `get_current_user()` in `deps.py` | Call `JitProvisioner.ensure_account()` after OIDC auth |
| `Org` model in `orgs/registry.py` | Add `dedicated_database`, internal metadata fields |
| `Group` model in `auth/groups.py` | Add `org_ids` field |
| Orgs API router `v1/orgs.py` | Wire `OrgLifecycleManager`, add `confirm_merge` guard |
| `Account` model in `auth/accounts.py` | Add `external`, `source_provider`, `last_login_at` fields |
| `HyperDXClient` | Add `delete_team()`, `update_connection()` methods |

### Design Principles

- **Non-fatal provisioning** — CH or HyperDX failures log warnings but do
  not block the primary operation
- **Control plane, not critical path** — if dfe-engine is down, existing
  CH users, row policies, and HyperDX teams keep working
- **Idempotent** — re-running provisioning on an existing org is safe
- **Custom settings for shared DB** — no per-org CH objects for the common
  case. Per-org users only for dedicated databases.

---

## 7. Testing

### Unit Tests (No External Deps)

| Component | What | How |
|-----------|------|-----|
| `OrgChProvisioner` | SQL generation, user naming | Verify SQL strings (no CH connection) |
| `OrgLifecycleManager` | Orchestration order, error handling | Skip CH/HyperDX calls in test mode |
| `JitProvisioner` | First login, race condition, broadest-wins, external flag | Real AccountStore + GroupStore (tmp_path) |
| `get_current_user()` JIT | OIDC triggers provisioning, no-op on existing | TestClient with OIDC headers |
| Org API | `dedicated_database` toggle, `confirm_merge` guard | TestClient E2E |

### Integration Tests (Requires CH)

| Test | Validates |
|------|-----------|
| Create org with dedicated DB → verify CH user + database | Real DDL execution |
| Dedicated DB user has full access, no cross-org leakage | Query isolation |
| Toggle dedicated DB off → routing change, DB preserved | Lifecycle |
| Delete org → CH user dropped, DB preserved | Cleanup |

### E2E Test (TestClient)

Create org → OIDC login as new user → verify shadow account created →
verify correct team assignment → disable org → verify account still works.

---

## 8. Future Work (Add to TODO)

- **Core table/view management from dfe-engine** — the schema bootstrap
  code (`SchemaBuilderV2`, `DDLFileWriter`, `DDLManager`) currently targets
  a single database. Needs to support deploying the default table+view set
  to any database (shared or dedicated org DB). Until this is done,
  `dedicated_database: true` requires manual schema deployment in the
  dedicated DB.
