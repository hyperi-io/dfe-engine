# RBAC, Multi-Tenant ClickHouse, and Rust App Layer

**Status:** Design (not yet implemented)
**Scope:** Auth overhaul, granular RBAC, multi-tenant CH with row-level security, schema-less Rust service discovery, HyperDX integration

---

## 1. Problem Statement

DFE 2.2 needs:

1. **Production auth via Envoy Gateway OIDC** with standalone JWT fallback and API keys for M2M
2. **Granular RBAC** — roles built from fine-grained permissions, assigned via OIDC groups or local users
3. **Multi-tenant ClickHouse** — row-level security via custom settings pattern, per-role connections
4. **Zero-code Rust service management** — adding a new dfe-* service requires zero Python code
5. **HyperDX connection sync** — dfe-engine as single source of truth for all CH connections

Current state: 4 hardcoded roles, single CH connection, typed plugin models per Rust service, no OIDC header support, no HyperDX integration.

---

## 2. Auth Flow

### 2.1 Four Authentication Paths

```
Request arrives at dfe-engine
    |
    +-- Has X-Oidc-Subject header? (Envoy Gateway fronted)
    |   -> Extract user_id from X-Oidc-Subject
    |   -> Extract groups from X-Oidc-Groups (comma-separated)
    |   -> Look up group->role assignments from rbac/assignments.yaml
    |   -> Resolve org_ids for customer-scoped roles
    |   -> Build AuthContext(user_id, roles, groups, org_ids)
    |
    +-- Has X-API-Key header? (machine-to-machine)
    |   -> Extract short token from key prefix, look up in accounts
    |   -> SHA-256 verify long token against stored hash
    |   -> Resolve account name -> roles from assignments.yaml
    |   -> Build AuthContext(user_id=account_name, roles, org_ids)
    |
    +-- Has Authorization: Bearer token? (standalone users)
    |   -> Decode JWT (PyJWT)
    |   -> Extract user_id from "sub" claim
    |   -> Extract roles, org_ids from claims
    |   -> Build AuthContext
    |
    +-- None of the above, auth.enabled=False? (dev/test)
    |   -> Return root AuthContext(roles=["admin"], org_ids=["*"])
    |
    +-- None of the above, auth.enabled=True?
        -> 401 Unauthorized
```

| Path | Use case | Credential storage | Token lifetime |
|------|----------|-------------------|----------------|
| OIDC headers | Production (Envoy fronted) | IdP (Entra, Google, etc.) | Session cookie (Envoy managed) |
| API key | CI/CD, Terraform, scripts | `local_accounts.yaml` (SHA-256 hash) | Long-lived (no expiry, revoke by removing) |
| JWT Bearer | Standalone UI, dev | Issued by `/api/v1/auth/login` | `jwt_expire_minutes` (default 30) |
| Disabled | Dev/test | N/A | N/A |

OIDC headers injected by Envoy Gateway:

| Header | Content |
|--------|---------|
| `X-Oidc-Subject` | User email or unique ID |
| `X-Oidc-Groups` | Comma-separated OIDC group names |
| `X-Forwarded-User` | Same as subject (nginx compat) |

### 2.2 Deployment Modes

Local auth is **always available**. OIDC is additive — it does not replace
local auth. You can enable/disable OIDC without losing break-glass access.

| Mode | Setting | What's active | Use case |
|------|---------|--------------|----------|
| **Dev/test** | `auth.enabled=false` | All requests get root admin context | Local development |
| **Standalone (Docker)** | `auth.enabled=true` | JWT Bearer + API keys + local accounts | Small deploy, no external IdP |
| **Production (+ OIDC)** | `auth.enabled=true`, `auth.mode=oidc` | OIDC headers (precedence) + JWT + API keys + local | K8s with Envoy Gateway |

In production with OIDC, the detection order means:
- Browser users → Envoy handles OIDC → OIDC headers reach dfe-engine
- API clients → send JWT Bearer or API key directly (bypass Envoy OIDC)
- Break-glass → local login via `/api/v1/auth/login` → JWT Bearer

Switching from standalone to OIDC = set `auth.mode=oidc` and configure Envoy.
Switching back = set `auth.mode=jwt`. Local accounts and API keys keep working.

### 2.3 OIDC Header Trust Model

**Security precondition:** When `auth.mode=oidc`, dfe-engine MUST only be
accessible via Envoy Gateway. Direct pod access MUST be blocked by K8s
NetworkPolicy (deployed by dfe-infra `network-policies` chart). Without this,
any client with pod access can forge OIDC headers and impersonate any user.

When `auth.mode=oidc`, the OIDC header path is primary and JWT Bearer + API
key are still accepted for programmatic clients.

### 2.4 Credential Storage Architecture

**Passwords and API keys are NEVER stored in YAML.** YAML contains only
references (env var names) and non-reversible hashes. Actual secrets live in
the deployment's secrets backend.

#### Where Secrets Live (by deployment mode)

| Deployment | Secrets Backend | How dfe-engine reads secrets |
|------------|----------------|----------------------------|
| **K8s production** | OpenBao / cloud SM → ESO → K8s Secret | Mounted as env vars in pod spec |
| **K8s standalone** | K8s Secret (manual or Helm values) | Mounted as env vars in pod spec |
| **Docker** | `.env` file (gitignored) or compose env vars | `os.environ` |
| **Dev/test** | `.env` file or shell exports | `os.environ` |

**K8s secrets flow:**
```
OpenBao (or AWS SM / GCP SM / Azure KV)
    → ESO ClusterSecretStore syncs to K8s Secret
        → Pod spec mounts Secret as env vars
            → dfe-engine reads os.environ at startup
```

#### What Goes Where

| Data | Where stored | Format | Safe to commit? |
|------|-------------|--------|-----------------|
| Account names | `local_accounts.yaml` | Plaintext | Yes |
| Password env var names | `local_accounts.yaml` | `password_env: DFE_ADMIN_PASSWORD` | Yes (just a pointer) |
| Actual passwords | Env var / K8s Secret / OpenBao | Plaintext or bcrypt hash | **NO** |
| API key short token | `local_accounts.yaml` | Plaintext (8 chars, lookup only) | Yes |
| API key long token hash | `local_accounts.yaml` | `sha256:e3b0c442...` | Yes (non-reversible) |
| Full API key | Shown once at creation | `dfe_ak_live_8a3f_7f3b2c...` | **NO** (never stored) |
| JWT signing secret | Env var (`DFE_API_JWT_SECRET`) | Random 256-bit | **NO** |
| CH connection passwords | Env var / K8s Secret | Plaintext | **NO** |

#### Config Files (safe to commit to git)

```
config/rbac/
    roles.yaml              # Role definitions (permissions per role)
    assignments.yaml        # user/group -> role mapping (+ org_ids)
    local_accounts.yaml     # Account metadata (env var refs + API key hashes)
```

**`local_accounts.yaml`** — defines who can authenticate:

```yaml
# Accounts for local auth (standalone) and API key auth (all modes).
# NO SECRETS IN THIS FILE. Passwords read from env vars at runtime.
# API key hashes are SHA-256 (non-reversible, safe to commit).
# Role assignments come from assignments.yaml, NOT from this file.
accounts:
  # Password accounts (local auth + standalone JWT login)
  admin:
    password_env: DFE_ADMIN_PASSWORD          # env var -> plaintext or bcrypt hash
  operator:
    password_env: DFE_OPERATOR_PASSWORD
  viewer:
    password_env: DFE_VIEWER_PASSWORD
  acme_viewer:
    password_env: DFE_ACME_VIEWER_PASSWORD

  # API key accounts (all deployment modes including K8s)
  ci_deployer:
    api_key_short_token: "8a3f2c91"           # Lookup index (safe to commit)
    api_key_hash: "sha256:e3b0c442..."        # SHA-256 of long token (safe to commit)
  terraform_svc:
    api_key_short_token: "b7d4e1a3"
    api_key_hash: "sha256:a1b2c3d4..."
```

#### Corresponding Secrets (NOT committed)

```bash
# .env (Docker standalone) or K8s Secret (production)
DFE_ADMIN_PASSWORD="$2b$12$LJ3..."        # bcrypt hash for production
DFE_OPERATOR_PASSWORD="changeme"           # plaintext OK for dev only
DFE_VIEWER_PASSWORD="changeme"
DFE_ACME_VIEWER_PASSWORD="$2b$12$xyz..."
DFE_API_JWT_SECRET="random-256-bit-secret"
CH_ADMIN_PASSWORD="clickhouse-admin-pw"
CH_ANALYST_PASSWORD="clickhouse-analyst-pw"
CH_ANALYST_RO_PASSWORD="clickhouse-ro-pw"
CH_VIEWER_PASSWORD="clickhouse-viewer-pw"
CH_TENANT_READER_PASSWORD="clickhouse-tenant-pw"
```

#### Password Verification Flow

```python
# LocalAuthProvider.authenticate()
password_env = account["password_env"]           # e.g. "DFE_ADMIN_PASSWORD"
stored_value = os.environ[password_env]           # e.g. "$2b$12$LJ3..." or "changeme"

if is_bcrypt_hash(stored_value):
    # Production: env var contains pre-hashed bcrypt
    bcrypt.checkpw(submitted_password, stored_value)
else:
    # Dev: env var contains plaintext, hash on the fly
    bcrypt.checkpw(submitted_password, bcrypt.hashpw(stored_value))
```

#### API Key Verification Flow

```python
# verify_api_key()
# Input: "dfe_ak_live_8a3f2c91_7f3b2c4d8e9a1b5f..."
prefix, short_token, long_token = parse_api_key(submitted_key)

# 1. Look up by short_token (fast, indexed)
account = find_account_by_short_token(short_token)

# 2. SHA-256 verify (no bcrypt — key is high-entropy random)
expected_hash = account["api_key_hash"]  # "sha256:e3b0c442..."
actual_hash = "sha256:" + hashlib.sha256(long_token.encode()).hexdigest()
if not hmac.compare_digest(expected_hash, actual_hash):
    raise AuthenticationError("Invalid API key")
```

**Separation of concerns:**
- `local_accounts.yaml` — WHO can authenticate (account names + env var refs + hashes)
- `assignments.yaml` — WHAT roles they get (account/group → DFE role mapping)
- `roles.yaml` — WHAT permissions those roles have
- Secrets backend (env vars / K8s Secrets / OpenBao) — actual passwords and CH credentials

### 2.5 API Key Format

Following industry best practice (Stripe, GitHub, Seam pattern):

```
dfe_ak_live_8a3f2c91_7f3b2c4d8e9a1b5f6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f
|           |         |
prefix      short     long token (shown once at creation, stored as SHA-256 hash)
            token
            (lookup index, shown in UI)
```

- **Prefix** (`dfe_ak_live_` / `dfe_ak_test_` / `dfe_sk_live_`): enables secret scanning by GitHub, GitGuardian, etc.
- **Short token** (8 chars): plaintext in DB, used for lookup and display in UI
- **Long token** (32+ chars): SHA-256 hashed in DB. SHA-256 not bcrypt because API keys are high-entropy random strings — bcrypt's slowness is unnecessary overhead.
- Full key shown **once** at creation, never again.
- **Rotation:** 90-day recommended, with 7-day grace period (old + new both valid).
- **Revocation:** Remove account from `local_accounts.yaml` or delete the key entry.

### 2.6 AuthContext Changes

```python
class AuthContext(BaseModel):
    org_id: str = "default"  # Primary tenant: first org_id or "default" for non-scoped
    user_id: str
    roles: list[str]         # Resolved DFE roles
    groups: list[str]        # Raw OIDC groups
    org_ids: list[str] = []  # For customer-scoped roles
    connection_id: str = ""  # Resolved CH connection name
    request_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
```

**Removed from current model:** `permissions: list[str]` — permissions are
resolved at authorization time from roles, never stored on the context.

**`org_id` resolution rule:**
- Customer-scoped roles: `org_id = org_ids[0]` (primary org from the list)
- Non-scoped roles (admin, analyst, etc.): `org_id = "default"`
- OIDC token with `org_id` claim: use the claim value directly

---

## 3. RBAC Data Model

All RBAC config lives in the config cascade under `config/rbac/`.

### 3.1 Role Definitions (roles.yaml)

Roles are collections of granular permission strings. Permission format:
`{domain}:{action}` or `{domain}:{resource}:{action}` for scoped access.

```yaml
roles:
  admin:
    description: "Full access to all resources and all orgs"
    permissions: ["*"]

  data_analyst:
    description: "Hunt, query, source, fieldmap CRUD — all orgs"
    permissions:
      - "hunt:*"
      - "query:*"
      - "source:*"
      - "fieldmap:*"
      - "alert:*"
      - "schema:read"
      - "transforms:*"

  data_analyst_viewer:
    description: "Same scope as data_analyst, read-only"
    permissions:
      - "hunt:read"
      - "query:read"
      - "query:execute"
      - "source:read"
      - "fieldmap:read"
      - "alert:read"
      - "schema:read"

  data_viewer:
    description: "HyperDX dashboards and query execution — all orgs"
    permissions:
      - "query:execute"
      - "source:read"
      - "dashboard:read"

  infra_admin:
    description: "Service configs, deployments, helm, Argo CD — full CRUD"
    permissions:
      - "config:*"
      - "service:*:config:*"
      - "service:*:metrics:read"
      - "helm:*"
      - "deployment:*"
      - "argo:*"

  infra_viewer:
    description: "Infrastructure read-only"
    permissions:
      - "config:read"
      - "service:*:config:read"
      - "service:*:metrics:read"
      - "helm:compile"
      - "deployment:read"
      - "argo:applications:get"
      - "argo:projects:get"

  customer_viewer:
    description: "Org-restricted data viewer (dynamic, instantiated per customer)"
    permissions:
      - "query:execute"
      - "source:read"
      - "dashboard:read"
    scoped: true  # Indicates this role requires org_ids
```

**Role hierarchy:** Flat (no inheritance). The 7-role set does not justify
inheritance complexity. Revisit if role count exceeds ~20.

### 3.2 Permission Taxonomy

| Domain | Actions | Scoped by service? |
|--------|---------|---------------------|
| `hunt` | `read`, `write`, `execute`, `*` | No |
| `query` | `read`, `execute`, `*` | No |
| `source` | `read`, `write`, `*` | No |
| `fieldmap` | `read`, `write`, `*` | No |
| `alert` | `read`, `write`, `*` | No |
| `schema` | `read`, `write`, `*` | No |
| `dashboard` | `read`, `write`, `*` | No |
| `config` | `read`, `write`, `*` | No |
| `transforms` | `compile`, `test`, `*` | No |
| `service` | `config:read`, `config:write`, `metrics:read`, `*` | Yes (`service:{name}:{action}`) |
| `helm` | `compile`, `execute_ddl`, `create_topics`, `*` | No |
| `deployment` | `read`, `write`, `*` | No |
| `org` | `read`, `write`, `*` | No |
| `argo` | `{resource}:{action}` (open-ended) | No |

Wildcard rules:
- `"*"` grants everything (admin only)
- `"service:*:config:read"` grants config read on all services
- `"service:dfe-loader:config:write"` grants write on one service
- `"argo:*"` grants all Argo CD actions

### 3.3 Role Assignments (assignments.yaml)

```yaml
# OIDC group -> DFE role binding
groups:
  "platform-admins@hypersec.io": [admin]
  "soc-team@hypersec.io": [data_analyst]
  "soc-readonly@hypersec.io": [data_analyst_viewer]
  "infra-team@hypersec.io": [infra_admin]
  "infra-readonly@hypersec.io": [infra_viewer]

  # Customer-scoped: includes org_ids restriction
  "acme-soc@acme.com":
    roles: [customer_viewer]
    org_ids: ["acme", "acme-subsidiary"]

# Local user -> DFE role binding (break-glass / standalone)
users:
  admin: [admin]
  operator: [data_analyst, infra_admin]
  viewer: [data_viewer]
  ci_deployer: [infra_admin]
  terraform_svc: [infra_admin]
```

### 3.4 Authorization Engine

The existing `authorize()` function evolves to load roles from YAML and
support wildcard permission matching:

```python
def permission_matches(permission: str, action: str) -> bool:
    if permission == "*":
        return True
    if "*" not in permission:
        return permission == action
    perm_parts = permission.split(":")
    action_parts = action.split(":")
    if len(perm_parts) != len(action_parts):
        return False
    return all(p == "*" or p == a for p, a in zip(perm_parts, action_parts))
```

`DEFAULT_ROLE_PERMISSIONS` constant is removed. `roles.yaml` is the sole source
of truth. Bootstrap defaults are seeded from a built-in YAML resource on first run.

---

## 4. ClickHouse Multi-Tenant Connection Registry

### 4.1 Custom Settings Pattern (not per-org users)

Instead of creating a CH user per customer org (which creates N users * T
tables of row policy objects), use the ClickHouse custom settings pattern:

```sql
-- ONE row policy per tenant-scoped table, referencing a custom setting
CREATE ROW POLICY tenant_filter ON dfe.events
    FOR SELECT USING org_id = getSetting('current_tenant_id')
    TO dfe_reader;

-- Application injects tenant per query via connection setting
SET current_tenant_id = 'acme';
SELECT * FROM dfe.events;  -- automatically filtered to acme rows
```

**Benefits over per-org users:**
- 3-5 CH users total (by privilege level), not N per org
- One row policy per tenant-scoped table (not per org)
- Scales to thousands of orgs without CH user sprawl
- dfe-engine injects `current_tenant_id` per query based on user's `org_ids`
- If the setting is omitted, the query fails (fail-closed)

**Which tables get row policies:** Only tables with an `org_id` column.
The reconciler discovers these via:
```sql
SELECT table FROM system.columns
WHERE database = 'dfe' AND name = 'org_id'
```

System, metadata, and audit tables are excluded.

### 4.2 Connection Model

```python
class ClickHouseConnection(BaseModel):
    name: str
    host: str
    port: int = 8123
    database: str = "dfe"
    user: str
    password_env: str          # ENV var name holding the password
    tenant_setting: str = ""   # If set, inject as current_tenant_id per query
```

### 4.3 Connections Config (connections.yaml)

```yaml
connections:
  # Admin: unrestricted, no tenant filter
  default:
    host: clickhouse.clickhouse.svc.cluster.local
    port: 8123
    database: dfe
    user: dfe_admin
    password_env: CH_ADMIN_PASSWORD

  # Analyst: read-write, no tenant filter
  analyst:
    host: clickhouse.clickhouse.svc.cluster.local
    port: 8123
    database: dfe
    user: dfe_analyst
    password_env: CH_ANALYST_PASSWORD

  # Read-only: no tenant filter
  analyst_ro:
    host: clickhouse.clickhouse.svc.cluster.local
    port: 8123
    database: dfe
    user: dfe_analyst_ro
    password_env: CH_ANALYST_RO_PASSWORD

  # Viewer: read-only, no tenant filter
  viewer:
    host: clickhouse.clickhouse.svc.cluster.local
    port: 8123
    database: dfe
    user: dfe_viewer
    password_env: CH_VIEWER_PASSWORD

  # Tenant-scoped reader: queries inject current_tenant_id
  tenant_reader:
    host: clickhouse.clickhouse.svc.cluster.local
    port: 8123
    database: dfe
    user: dfe_tenant_reader
    password_env: CH_TENANT_READER_PASSWORD

# Role -> connection mapping
role_connections:
  admin: default
  data_analyst: analyst
  data_analyst_viewer: analyst_ro
  data_viewer: viewer
  infra_admin: default
  infra_viewer: analyst_ro
  customer_viewer: tenant_reader   # tenant_id injected per query from org_ids
```

### 4.4 Connection Resolution

```python
class ConnectionRegistry:
    def get_connection(self, auth: AuthContext) -> ClickHouseClient:
        """Resolve correct CH connection for this user's role + org scope."""
        # Multi-role precedence: highest-privilege connection wins
        # Order: default > analyst > analyst_ro > viewer > tenant_reader
        conn_name = self._resolve_best_connection(auth.roles)
        conn = self._connections[conn_name]

        client = self._get_or_create_client(conn)

        # For tenant-scoped connections, wrap client to inject setting
        if conn_name == "tenant_reader" and auth.org_ids:
            return TenantScopedClient(client, org_ids=auth.org_ids)

        return client
```

`TenantScopedClient` wraps the CH client to prepend
`SET current_tenant_id = '{org_id}'` to every query. For users with multiple
`org_ids`, it uses `org_id IN (...)` in the setting.

### 4.5 Startup Reconciliation

On startup, dfe-engine ensures CH state matches config:

1. Ensure static CH users exist (`dfe_admin`, `dfe_analyst`, `dfe_analyst_ro`, `dfe_viewer`, `dfe_tenant_reader`)
2. Ensure row policies exist on all tenant-scoped tables
3. Ensure grants match expected levels
4. Bootstrap DDL templates come from dfe-schemas repo

If ClickHouse is unreachable at startup, dfe-engine starts in degraded mode
(API serves requests using cached connections.yaml, `/health/ready` returns
503, reconciliation retried every 60s).

### 4.6 Org Lifecycle

```
POST /api/v1/orgs { org_id: "acme", org_ids: ["acme", "acme-sub"] }
    -> OrgRegistry.create()
    -> No CH user creation needed (custom settings pattern)
    -> HyperDXClient.create_team("customer-acme")
    -> HyperDXClient.create_connection(team_id, tenant_reader + setting)
```

With the custom settings pattern, adding an org does NOT require creating a CH
user or row policy. The existing `tenant_reader` user + existing row policies
handle it. dfe-engine just needs to know the org_ids to inject per query.

### 4.7 Secrets Rotation

**Static connections:** Dual-user pattern for zero downtime. Maintain two CH
users per role (e.g., `dfe_admin_a`, `dfe_admin_b`), rotate one at a time.
Use Vault/OpenBao database secrets engine or K8s CronJob + ESO.

**Tenant reader connection:** Single shared user, rotated on schedule.
Stakater Reloader triggers pod restart when K8s Secret updates.

---

## 5. Rust App Layer (Schema-Less Service Discovery)

### 5.1 Principle

Adding a new `dfe-transform-elastic` requires:
1. Deploy the Rust service (Helm chart in dfe-infra)
2. That's it. Zero Python code changes.

dfe-engine discovers the service, its configurable settings, and its metrics
automatically. The list of services is itself a config cascade item.

### 5.2 Service Surface Registry

Replaces the typed plugin system. All service metadata is YAML.

```
config/service-surfaces/
    dfe-receiver.yaml
    dfe-loader.yaml
    ...
    dfe-transform-elastic.yaml   # Added by deploying the service
```

Each surface file describes what the service exposes:

```yaml
# dfe-loader.yaml
service: dfe-loader
description: "Kafka consumer -> ClickHouse writer"

config_surface:
  config.kafka.bootstrap_servers:
    type: string
    description: "Kafka bootstrap servers"
  config.buffer.max_bytes:
    type: integer
    description: "Buffer size limit in bytes"

metrics_surface:
  manifest_url: "http://dfe-loader.dfe-prod.svc.cluster.local:8080/metrics/manifest"
  metrics:   # Cached from /metrics/manifest
    - name: dfe_loader_records_received_total
      type: counter
      group: app
    - name: dfe_loader_buffer_flush_duration_seconds
      type: histogram
      group: buffer
```

### 5.3 Service Discovery Modes

**A) Static (config-driven):** Surface YAML files committed to config dir. Default.

**B) Dynamic (K8s-aware):** Future enhancement. dfe-engine watches for services
with label `dfe.hyperi.io/managed=true`, fetches `/metrics/manifest`, generates
surface YAML.

### 5.4 API Endpoints

```
GET  /api/v1/service-surfaces                         # List all discovered services
GET  /api/v1/service-surfaces/{name}                  # Service metadata
GET  /api/v1/service-surfaces/{name}/metrics          # Cached metrics manifest
POST /api/v1/service-surfaces/{name}/metrics/refresh  # Re-fetch manifest
```

Existing `/api/v1/services/{service}/{instance}` endpoints remain unchanged.

RBAC via `require_service_action()` dependency factory that constructs
`service:{name}:{action}` from the path parameter.

---

## 6. HyperDX Integration

### 6.1 Strategy

- **Bootstrap:** dfe-engine generates `DEFAULT_CONNECTIONS` JSON env var for HyperDX Helm chart
- **Runtime:** dfe-engine calls HyperDX internal API for team/connection CRUD
- **Auth:** Service account using HyperDX team API key (stored in K8s secret)
- **Failures:** Non-fatal, background retry reconciliation

### 6.2 Team Mapping

| DFE Role Scope | HyperDX Team | CH Connection | Tenant Setting |
|----------------|-------------|---------------|----------------|
| admin | `dfe-admin` | `default` | None (unrestricted) |
| data_analyst | `dfe-analysts` | `analyst` | None |
| data_viewer | `dfe-viewers` | `viewer` | None |
| customer_viewer (acme) | `customer-acme` | `tenant_reader` | `current_tenant_id=acme` |

### 6.3 Alignment with HyperDX

**Adopt:** Team API keys for M2M auth, connection model, ClickHouse proxy pattern.

**Do NOT adopt:** HyperDX anti-patterns (no internal RBAC, no per-user scoping,
all team members equal). We solve these at the dfe-engine layer.

---

## 7. Audit Logging

Every authorization decision MUST be logged for compliance (SOC 2, GDPR):

| Event | Logged Data |
|-------|------------|
| Login (success/failure) | Timestamp, user_id, auth_path, client_ip, user_agent |
| Permission denied | Timestamp, user_id, action, role, reason |
| Role assignment change | Timestamp, admin_id, target_user, old_roles, new_roles |
| Org CRUD | Timestamp, admin_id, org_id, action (create/update/delete) |
| API key created/revoked | Timestamp, admin_id, key_short_token, account_name |
| CH connection created | Timestamp, connection_name, trigger (startup/org_crud) |

**Storage:** ClickHouse `dfe_audit.auth_events` (ReplacingMergeTree) + external SIEM.
**Retention:** Minimum 1 year (SOC 2), 7 years (SOX if applicable).

The `require_action()` dependency is the interception point — log every
authorization check with full context.

---

## 8. Config Directory Layout

```
config/
    auth/
        accounts/               # NEW: one YAML per local account (bcrypt hashes)
            admin.yaml
            analyst1.yaml
        groups/                 # NEW: one YAML per group (group -> roles)
            dfe-admins.yaml
            soc-analysts.yaml
        api-keys/               # NEW: one YAML per API key (SHA-256 hashes)
            ci-deploy.yaml
    rbac/
        roles.yaml              # Role definitions (permissions)
        connections.yaml        # CH connections + role mapping
    services/                   # Existing: Rust service runtime configs
    service-surfaces/           # NEW: auto-discovered service metadata
    orgs/                       # NEW: org registry
    deployment/                 # Existing: K8s/KEDA deployment configs
    sources/                    # Existing: data source definitions
    fieldmaps/                  # Existing: field map definitions
    alert-destinations/         # Existing: alert routing
```

---

## 9. Account and Group CRUD

### 9.1 Storage Architecture

Accounts, groups, and API keys are stored as individual YAML files via
`DirectoryConfigStore` (one file per entity, in-memory cache, git-aware writes).

```
config/auth/
    accounts/
        admin.yaml
        analyst1.yaml
        acme_viewer.yaml
    groups/
        soc-analysts.yaml
        infra-ops.yaml
        acme-viewers.yaml
    api-keys/
        ci-deploy.yaml
        terraform-svc.yaml
```

**Security model:** bcrypt hashes (rounds=12) are stored in the YAML files.
bcrypt hashes are one-way and designed to be safe even if leaked (equivalent
to `/etc/shadow`). File permissions enforced at 0600. No application-level
encryption — bcrypt + file perms is sufficient for the account scale (< 100).
Plaintext passwords and API key long tokens are NEVER stored anywhere.

### 9.2 File Formats

```yaml
# config/auth/accounts/analyst1.yaml
enabled: true
password_hash: "$2b$12$LJ3m..."
groups: ["soc-analysts"]
created_at: "2026-03-31T02:00:00Z"
updated_at: "2026-03-31T02:00:00Z"
```

```yaml
# config/auth/groups/soc-analysts.yaml
description: "SOC analyst team"
roles: ["data_analyst"]
members: ["analyst1", "analyst2"]     # Redundant index (accounts are source of truth)
```

```yaml
# config/auth/api-keys/ci-deploy.yaml
enabled: true
short_token: "8a3f2c91"
key_hash: "sha256:e3b0c442..."
groups: ["infra-ops"]
created_at: "2026-03-31T02:00:00Z"
description: "CI/CD pipeline deployer"
```

Account filename = username (e.g. `analyst1.yaml`). Group filename = group name.
API key filename = account name. The filename IS the identity — never stored
inside the YAML (avoids the DirectoryConfigStore YAML 1.1 gotcha with identity
fields becoming booleans).

### 9.3 Store Modules

```
src/dfe_engine/auth/
    accounts.py      # AccountStore: CRUD for local user accounts
    groups.py        # GroupStore: CRUD for groups (group -> roles)
    api_keys.py      # APIKeyStore: CRUD for API keys
    local_provider.py  # Refactored: uses AccountStore + GroupStore
    engine.py        # Refactored: loads roles from roles.yaml
    models.py        # AuthContext, AuthzResult, etc.
```

**AccountStore API:**

```python
class AccountStore:
    def __init__(self, config_dir: Path):
        """Load accounts from config/auth/accounts/ via DirectoryConfigStore."""

    def create(self, username: str, password: str,
               groups: list[str] | None = None) -> Account:
        """Create account with bcrypt-hashed password. Raises if exists."""

    def get(self, username: str) -> Account | None:
        """Get account by username (filename lookup)."""

    def list(self) -> list[Account]:
        """List all accounts (password hashes excluded from response)."""

    def update(self, username: str, **fields) -> Account:
        """Update account fields (groups, enabled)."""

    def reset_password(self, username: str, new_password: str) -> None:
        """Replace password hash."""

    def delete(self, username: str) -> None:
        """Delete account file."""

    def verify_password(self, username: str, password: str) -> bool:
        """bcrypt verify submitted password against stored hash."""

    def resolve_roles(self, username: str, group_store: GroupStore) -> list[str]:
        """Resolve roles: account -> groups -> roles from group definitions."""
```

**GroupStore API:**

```python
class GroupStore:
    def __init__(self, config_dir: Path):
        """Load groups from config/auth/groups/ via DirectoryConfigStore."""

    def create(self, name: str, roles: list[str],
               description: str = "") -> Group:
        """Create group. Raises if exists."""

    def get(self, name: str) -> Group | None
    def list(self) -> list[Group]
    def update(self, name: str, **fields) -> Group
    def delete(self, name: str) -> None

    def add_member(self, group_name: str, username: str) -> None:
        """Add user to group (updates both group and account files)."""

    def remove_member(self, group_name: str, username: str) -> None:
        """Remove user from group."""
```

**APIKeyStore API:**

```python
class APIKeyStore:
    def __init__(self, config_dir: Path):
        """Load API keys from config/auth/api-keys/ via DirectoryConfigStore."""

    def create(self, name: str, groups: list[str] | None = None,
               description: str = "") -> tuple[APIKey, str]:
        """Create API key. Returns (metadata, full_key_shown_once)."""

    def verify(self, submitted_key: str) -> APIKey | None:
        """Parse prefix+short_token, SHA-256 verify long token."""

    def list(self) -> list[APIKey]:
        """List all keys (hashes excluded, short tokens included)."""

    def revoke(self, short_token: str) -> None:
        """Delete API key file."""
```

### 9.4 REST API

```
POST   /api/v1/auth/accounts                 # Create account
GET    /api/v1/auth/accounts                 # List accounts (no hashes)
GET    /api/v1/auth/accounts/{username}      # Get account detail
PUT    /api/v1/auth/accounts/{username}      # Update (groups, enabled)
POST   /api/v1/auth/accounts/{username}/reset-password  # Reset password
DELETE /api/v1/auth/accounts/{username}      # Delete account

POST   /api/v1/auth/groups                   # Create group
GET    /api/v1/auth/groups                   # List groups
GET    /api/v1/auth/groups/{name}            # Get group detail + members
PUT    /api/v1/auth/groups/{name}            # Update (roles, description)
POST   /api/v1/auth/groups/{name}/members    # Add member
DELETE /api/v1/auth/groups/{name}/members/{username}  # Remove member
DELETE /api/v1/auth/groups/{name}            # Delete group

POST   /api/v1/auth/api-keys                # Create key (returns full key ONCE)
GET    /api/v1/auth/api-keys                 # List keys (short tokens only)
DELETE /api/v1/auth/api-keys/{short_token}   # Revoke key
```

All endpoints require `admin` role (or `org:write` for org-scoped operations).
Password hashes and API key hashes are NEVER returned in API responses.

### 9.5 CLI Commands

The `dfe-api` entry point extends with account/group/key management:

```bash
# Account management
dfe-api accounts create analyst1 --groups soc-analysts
  # Prompts for password (or --generate-password for random)
  # Writes config/auth/accounts/analyst1.yaml

dfe-api accounts list
dfe-api accounts show analyst1
dfe-api accounts disable analyst1
dfe-api accounts enable analyst1
dfe-api accounts reset-password analyst1
  # Prompts for new password
dfe-api accounts delete analyst1

# Group management
dfe-api groups create soc-analysts --roles data_analyst
dfe-api groups list
dfe-api groups show soc-analysts
dfe-api groups add-member soc-analysts analyst1
dfe-api groups remove-member soc-analysts analyst1
dfe-api groups set-roles soc-analysts data_analyst data_analyst_viewer
dfe-api groups delete soc-analysts

# API key management
dfe-api api-keys create ci-deploy --groups infra-ops --description "CI pipeline"
  # Prints full key ONCE to stdout:
  # API Key: dfe_ak_live_8a3f2c91_7f3b2c4d8e9a1b5f6c7d8e9f...
  # Store this key securely — it cannot be retrieved again.

dfe-api api-keys list
dfe-api api-keys revoke 8a3f2c91
```

The CLI reads/writes the same YAML files as the API. Both use `AccountStore`,
`GroupStore`, `APIKeyStore` underneath. CLI is for operators; API is for the UI.

### 9.6 Identity Resolution Chain

```
Authentication (who are you?)
    OIDC: X-Oidc-Subject header -> user_id
    API key: X-API-Key header -> APIKeyStore.verify() -> account name
    JWT: Bearer token -> decode -> user_id from "sub" claim
    Login: POST /auth/login -> AccountStore.verify_password() -> JWT issued
        |
        v
Group resolution (what groups?)
    OIDC: X-Oidc-Groups header -> group names (from IdP)
    Local: AccountStore.get(username).groups -> group names (from YAML)
    API key: APIKeyStore.get(name).groups -> group names (from YAML)
        |
        v
Role resolution (what roles?)
    GroupStore.get(group_name).roles -> DFE role names
    Union of all roles from all groups
        |
        v
Permission check (can you do this?)
    roles.yaml: role -> permissions list
    authorize(auth, action) -> permission_matches() with wildcards
```

OIDC groups and local groups are unified — an OIDC group name that matches
a group file in `config/auth/groups/` inherits that group's roles. This means
the same `groups.yaml` files serve both OIDC and local auth paths.

### 9.7 Bootstrap Defaults

On first startup (empty `config/auth/` directory), dfe-engine seeds:

**Accounts:** `admin` (password from `DFE_ADMIN_PASSWORD` env var or "changeme")

**Groups:**
- `dfe-admins` -> roles: [admin]
- `dfe-analysts` -> roles: [data_analyst]
- `dfe-viewers` -> roles: [data_viewer]
- `dfe-infra` -> roles: [infra_admin]

**Assignments:** `admin` account added to `dfe-admins` group.

Startup logs a warning if any account uses the default "changeme" password.

---

## 10. New Modules

| Module | Purpose |
|--------|---------|
| `auth/accounts.py` | AccountStore: CRUD for local user accounts (bcrypt) |
| `auth/groups.py` | GroupStore: CRUD for groups (group -> roles mapping) |
| `auth/api_keys.py` | APIKeyStore: CRUD for API keys (SHA-256 hashes) |
| `connections/` | ConnectionRegistry, TenantScopedClient, CH reconciliation |
| `hyperdx/` | HyperDXClient for team/connection/source sync |
| `orgs/` | OrgRegistry, org CRUD with HyperDX lifecycle hooks |

### Modified Modules

| Module | Change |
|--------|--------|
| `auth/engine.py` | Load roles from YAML, wildcard permission matching, remove `DEFAULT_ROLE_PERMISSIONS` |
| `auth/models.py` | Add `org_ids`, `connection_id`; remove `permissions` |
| `auth/local_provider.py` | Rewrite: uses AccountStore + GroupStore for auth |
| `api/deps.py` | OIDC header + API key auth paths, `require_service_action()` |
| `api/v1/auth.py` | Account/group/API key CRUD endpoints |
| `api/__init__.py` | CLI: `dfe-api accounts`, `groups`, `api-keys` subcommands |
| `services/registry.py` | Remove typed plugins, schema-less only |
| `settings.py` | Auth config paths; remove `role_permissions`, `group_role_mapping` |

---

## 10. Migration Path

### Phase 1: RBAC Foundation + Account CRUD
- `AccountStore`, `GroupStore`, `APIKeyStore` (YAML-backed via DirectoryConfigStore)
- REST API for account/group/API key CRUD
- CLI: `dfe-api accounts`, `groups`, `api-keys` subcommands
- Rewrite `LocalAuthProvider` to use AccountStore + GroupStore
- Replace `DEFAULT_ROLE_PERMISSIONS` with `roles.yaml`
- Remove `AuthSettings.role_permissions` and `group_role_mapping` from settings.py
- Remove `permissions` field from `AuthContext`
- Add OIDC header + API key auth paths to `get_current_user()`
- Wildcard permission matching in `authorize()`
- Bootstrap defaults seeded on first run
- Audit logging on all auth decisions

### Phase 2: Connection Registry
- ConnectionRegistry with custom settings pattern (3-5 CH users, not per-org)
- TenantScopedClient injects `current_tenant_id` per query
- Startup reconciliation (ensure CH users + row policies exist)
- Bootstrap DDL templates from dfe-schemas

### Phase 3: Org Lifecycle + HyperDX
- Org CRUD API with HyperDX team/connection sync
- HyperDXClient module
- `DEFAULT_CONNECTIONS` generation for Helm bootstrap

### Phase 4: Schema-Less Service Discovery
- Service surface YAML files
- Metrics manifest caching from rustlib `/metrics/manifest`
- `/api/v1/service-surfaces/` endpoints with RBAC
- Remove typed plugin system (`plugins.py`, `plugins_builtin/`)

---

## 11. Edge Cases and Error Handling

### Connection Resolution for Multi-Role Users

Precedence (highest first): `default` > `analyst` > `analyst_ro` / `viewer` > `tenant_reader`.
For customer-scoped roles, `tenant_reader` is used with tenant ID injection.

### HyperDX Unavailable

Non-fatal. Org CRUD succeeds. Failed HyperDX sync queued for background retry.

### ClickHouse Unavailable at Startup

Degraded mode: API serves requests with cached state, `/health/ready` returns 503,
reconciliation retried every 60s. `/health/live` returns 200.

### CH Row Policy on Tables Without org_id

Row policies ONLY created on tables with an `org_id` column (discovered via
`system.columns`). System, metadata, and audit tables are excluded.

---

## 12. Non-Goals

- Per-metric RBAC granularity (access is per-service, not per-metric)
- Per-setting RBAC granularity (access is per-service config, not per-key)
- HyperDX per-user RBAC (solved at dfe-engine layer via teams)
- Dynamic K8s service discovery (future enhancement)
- Envoy Gateway SecurityPolicy CRD generation (managed by dfe-infra)
- ClickHouse cluster provisioning (managed by dfe-infra)
- Role hierarchy / inheritance (flat roles sufficient for 7-role set)
- SPIFFE/SPIRE for S2S auth (future; API keys with best practices for now)

---

## 13. Breaking Changes (for Kay and Kaz)

DFE 2.2 is pre-GA. These are intentional breaking changes, not regressions.

| What Changed | Old (2.1 / pre-2.2) | New (2.2) | Migration |
|-------------|---------------------|-----------|-----------|
| **JWT library** | `python-jose[cryptography]` | `PyJWT[crypto]` | Already done (v1.7.3). Import changes only. |
| **Auth model** | `AuthContext.permissions` field | Removed | Permissions resolved from roles at auth time, not stored |
| **Role names** | `admin`, `infra_admin`, `operator`, `viewer` | `admin`, `data_analyst`, `data_analyst_viewer`, `data_viewer`, `infra_admin`, `infra_viewer`, `customer_viewer` | Old roles gone. New roles in `roles.yaml`. |
| **Role storage** | `DEFAULT_ROLE_PERMISSIONS` constant in code | `config/rbac/roles.yaml` | Hardcoded constant removed |
| **Account storage** | 3 hardcoded accounts in `LocalAuthProvider.ACCOUNTS` | `config/auth/accounts/*.yaml` (CRUD via API + CLI) | Hardcoded dict removed. AccountStore with full CRUD. |
| **Group mapping** | `AuthSettings.group_role_mapping` in settings.py | `config/rbac/assignments.yaml` | Settings field removed |
| **Auth paths** | JWT Bearer only | OIDC headers + API key + JWT Bearer | New paths additive, JWT unchanged |
| **CH connections** | Single global client | `ConnectionRegistry` (multi-client, tenant-scoped) | New module, old `clickhouse/` module refactored |
| **Service plugins** | Typed Pydantic models per Rust service | Schema-less YAML surfaces | `plugins.py` and `plugins_builtin/` removed |
| **Deep merge** | `deepmerge` pip package | `dfe_engine.yaml_utils.deep_merge()` (vendored) | Already done (v1.7.3) |

---

## 14. Research References

- [ClickHouse Custom Settings + Row Policy (LaunchDarkly/Highlight)](https://www.highlight.io/blog/row-level-security)
- [API Key Prefix Pattern (Seam)](https://github.com/seamapi/prefixed-api-key)
- [Multi-Tenant RBAC Design (WorkOS)](https://workos.com/blog/how-to-design-multi-tenant-rbac-saas)
- [Envoy Gateway OIDC SecurityPolicy](https://gateway.envoyproxy.io/docs/tasks/security/oidc/)
- [GitHub Actions OIDC Federation](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/about-security-hardening-with-openid-connect)
