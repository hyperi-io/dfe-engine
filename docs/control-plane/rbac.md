# RBAC, Multi-Tenant ClickHouse, and Auth Architecture

**Scope:** Auth, granular RBAC, multi-tenant CH with row-level security, OIDC group sync, HyperDX integration, Argo CD RBAC export

---

## Implementation Status

| Phase | Scope | Status |
|-------|-------|--------|
| **Phase 1** | RBAC foundation, account/group/API key CRUD, 4 auth paths, audit | Done |
| **Phase 2** | Per-org pinned ClickHouse users, shared tenant row policies, quota tiers | Done |
| **Phase 3** | OrgRegistry, HyperDX team/connection sync | Done |
| **Phase 4** | Schema-less service discovery, service surfaces | In progress - service surfaces shipped (`services/surfaces/registry.py`, `/api/v1/service-surfaces`); typed plugin removal pending |

---

## 1. Auth Flow

### 1.1 Four Authentication Paths

```mermaid
flowchart TD
    REQ[Incoming Request] --> OIDC{X-Oidc-Subject<br/>header?}
    OIDC -->|Yes| EXTRACT_OIDC[Extract user_id + groups<br/>from OIDC headers]
    EXTRACT_OIDC --> RESOLVE_OIDC[GroupStore resolves<br/>groups → roles]
    RESOLVE_OIDC --> CTX[Build AuthContext]

    OIDC -->|No| APIKEY{X-API-Key<br/>header?}
    APIKEY -->|Yes| VERIFY_KEY[APIKeyStore.verify<br/>Parse short+long token<br/>SHA-384 compare]
    VERIFY_KEY --> RESOLVE_KEY[GroupStore resolves<br/>key groups → roles]
    RESOLVE_KEY --> CTX

    APIKEY -->|No| JWT{Authorization:<br/>Bearer?}
    JWT -->|Yes| DECODE["Verify signature ES384 by default,<br/>issuer and expiry<br/>Take sub and org_id"]
    DECODE --> BIND["Bind sub to its account<br/>disabled or blocked -> 401"]
    BIND --> RESOLVE_JWT["GroupStore resolves the account's<br/>groups -> roles + org_ids<br/>token claims ignored"]
    RESOLVE_JWT --> CTX

    JWT -->|No| DISABLED{auth.enabled<br/>= false?}
    DISABLED -->|Yes| ROOT[Root context<br/>roles=admin<br/>user_id=dev]
    ROOT --> CTX

    DISABLED -->|No| REJECT[401 Unauthorized]

    CTX --> AUTH_DONE[AuthContext ready]

    style REJECT fill:#f44,color:#fff
    style AUTH_DONE fill:#4a4,color:#fff
    style ROOT fill:#fa0,color:#fff
```

| Path | Use case | Credential storage | Token lifetime |
|------|----------|-------------------|----------------|
| OIDC headers | Production (Envoy Gateway fronted) | IdP (Entra, Google, etc.) | Session cookie (Envoy managed) |
| API key | CI/CD, Terraform, scripts | `config/auth/api-keys/*.yaml` (SHA-384 hash) | `expires_at` if set, else long-lived (revoke by deleting file) |
| JWT Bearer | Console sessions, standalone, dev | Issued by `POST /api/v1/auth/login`, the OIDC login callback and `POST /api/v1/auth/refresh` | `api.jwt_expire_minutes` (`DFE_API_JWT_EXPIRE_MINUTES`, default 60) |
| Disabled | Dev/test | N/A | N/A |

A JWT carries identity, not authority. Its `roles`, `groups` and `org_ids` claims grant nothing: every request re-resolves them from the account the `sub` binds to, so a role or org taken away is gone from the next request. A `sub` that binds no account holds nothing -- an API-key subject, a deleted account -- and with auth enabled `POST /api/v1/auth/refresh` answers 401 for it, since each refresh issues a new expiry.

OIDC headers injected by Envoy Gateway SecurityPolicy:

| Header | Content |
|--------|---------|
| `X-Oidc-Subject` | User email or unique ID |
| `X-Oidc-Groups` | Comma-separated OIDC group names |

### 1.2 Deployment Modes

Local auth is **always available**. OIDC is additive — headers are checked
first when present, but JWT and API key paths remain active. No explicit
mode toggle; OIDC detection is automatic based on header presence.

| Mode | Setting | What's active | Use case |
|------|---------|--------------|----------|
| **Dev/test** | `auth.enabled=false` | All requests get root admin context | Local development |
| **Standalone** | `auth.enabled=true` | JWT Bearer + API keys + local accounts | Docker, no external IdP |
| **Production** | `auth.enabled=true` + Envoy | OIDC headers (precedence) + JWT + API keys | K8s with Envoy Gateway |

### 1.3 OIDC Header Trust Model

**Security precondition:** When deploying behind Envoy Gateway OIDC,
dfe-engine MUST only be accessible via Envoy. Direct pod access MUST be
blocked by K8s NetworkPolicy. Without this, any client with pod access can
forge OIDC headers and impersonate any user.

### 1.4 Credential Storage Architecture

```mermaid
flowchart LR
    subgraph "YAML Config (safe to commit)"
        ACC["accounts/*.yaml<br/>username, bcrypt hash,<br/>groups an IdP asserts"]
        GRP["groups/*.yaml<br/>group name, roles,<br/>member list"]
        KEY["api-keys/*.yaml<br/>short token, SHA-384 hash,<br/>group memberships"]
        ROLES["roles.yaml<br/>role definitions,<br/>permission patterns"]
    end

    subgraph "Secrets Backend (never committed)"
        ENV[".env / K8s Secret /<br/>OpenBao / Cloud SM"]
    end

    subgraph "Runtime"
        API["dfe-engine API"]
    end

    ACC -->|bcrypt verify| API
    GRP -->|membership +<br/>role lookup| API
    KEY -->|SHA-384 verify| API
    ROLES -->|permission check| API
    ENV -->|JWT signing key,<br/>CH passwords| API
```

#### What Goes Where

| Data | Where stored | Format | Safe to commit? |
|------|-------------|--------|-----------------|
| Account names | `accounts/{name}.yaml` | Filename stem | Yes |
| Password hashes | `accounts/{name}.yaml` | `$2b$12$...` (bcrypt) | Yes (one-way) |
| API key short token | `api-keys/{name}.yaml` | Plaintext (8 hex chars) | Yes (lookup index) |
| API key long hash | `api-keys/{name}.yaml` | `sha384:{hex}` (legacy `sha256:` keys still verify) | Yes (one-way) |
| Full API key | Shown once at creation | `dfe_ak_{short}_{long}` | **NO** (never stored) |
| JWT signing key | Secrets backend at `api.jwt_key_path` (default `jwt/signing-key`), minted on first boot when absent | ECDSA private key, PEM (P-384 for the default ES384) | **NO** |
| OIDC login session cookie key | `DFE_API_SESSION_SECRET`, else `DFE_API_JWT_SECRET` | Random, `DFE_API_JWT_SECRET` at least 32 bytes | **NO** |
| CH connection passwords | Env var / K8s Secret | Plaintext | **NO** |
| Role definitions | `auth/resources/roles.yaml` | Permission patterns | Yes |
| Group→role mapping | `groups/{name}.yaml` | Role list | Yes |

#### Config Directory Layout

```
config/auth/
    accounts/           # One YAML per local user account
        admin.yaml
        analyst1.yaml
    groups/              # One YAML per group (group → roles)
        dfe-admins.yaml
        soc-analysts.yaml
    api-keys/            # One YAML per API key
        ci-deploy.yaml
```

Account filename = username. Group filename = group name. API key filename =
key name. The filename IS the identity — never stored inside the YAML body
(avoids DirectoryConfigStore YAML 1.1 boolean coercion on values like `off`,
`yes`, `no`).

### 1.5 Password Verification Flow

```python
# AccountStore.verify_password()
account = self.get(username)
if account is None:
    bcrypt.checkpw(password.encode(), _DUMMY_HASH)  # Timing-safe rejection
    return False
return bcrypt.checkpw(password.encode(), account.password_hash.encode())
```

Constant-time rejection for unknown usernames prevents enumeration attacks.

### 1.6 API Key Format

```
dfe_ak_8a3f2c91_7f3b2c4d8e9a1b5f6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f
|      |         |
prefix short     long token (shown once, stored as SHA-384 hash)
       token
       (lookup index, shown in UI)
```

- **Prefix** (`dfe_ak_`): enables secret scanning by GitHub, GitGuardian
- **Short token** (8 hex chars): plaintext in YAML, used for lookup and display
- **Long token** (32 hex chars): SHA-384 hashed. Not bcrypt -- keys are
  high-entropy random, bcrypt's slowness adds no security value
- Full key shown **once** at creation, never retrievable again
- **Expiry** (`expires_at`, optional ISO-8601 stored as UTC): enforced on every
  verify, so a lapsed key stops authenticating with no sweeper running. The
  file stays until revoked, and `list` reports `expired: true`
- **Revocation:** Delete the key's YAML file or call the revoke API

### 1.7 API Key Verification Flow

```python
# APIKeyStore.verify()
# Input: "dfe_ak_8a3f2c91_7f3b2c4d8e9a1b5f..."
prefix, short_token, long_token = parse_api_key(submitted_key)

# 1. Scan for matching short_token across key files
key_meta = find_by_short_token(short_token)

# 2. Hash verify, timing-safe (SHA-384; legacy sha256: keys still verify)
actual_hash = "sha384:" + hashlib.sha384(long_token.encode()).hexdigest()
if not hmac.compare_digest(key_meta.key_hash, actual_hash):
    raise AuthenticationError("Invalid API key")

# 3. Status checks run only AFTER possession is proven, so the specific
#    reason cannot confirm a key the caller does not already hold
if not key_meta.enabled:
    raise AuthenticationError("API key disabled")
if key_meta.is_expired():
    raise AuthenticationError("API key expired")
```

---

## 2. RBAC Data Model

### 2.1 Identity Resolution Chain

```mermaid
flowchart TD
    subgraph "Authentication (who are you?)"
        OIDC_AUTH["OIDC: X-Oidc-Subject → user_id"]
        KEY_AUTH["API Key: X-API-Key → APIKeyStore.verify() → key name"]
        JWT_AUTH["JWT: Bearer -> verify -> sub binds an account"]
        LOCAL_AUTH["Login: POST /auth/login → AccountStore.verify_password()"]
    end

    subgraph "Group Resolution (what groups?)"
        OIDC_GRP["OIDC: X-Oidc-Groups header"]
        ACCT_GRP["Account: the group files listing it as a member,<br/>plus its IdP-asserted groups when an IdP owns it"]
        KEY_GRP["API Key: key.groups field"]
    end

    subgraph "Role Resolution (what roles?)"
        ROLE_RES["Member groups by name, IdP identifiers by linked source_id<br/>Union of roles, scoped grants and org_ids"]
    end

    subgraph "Permission Check (can you do this?)"
        PERM["authorize(auth, action)<br/>permission_matches() with wildcards"]
    end

    OIDC_AUTH --> OIDC_GRP
    KEY_AUTH --> KEY_GRP
    JWT_AUTH --> ACCT_GRP
    LOCAL_AUTH --> ACCT_GRP

    OIDC_GRP --> ROLE_RES
    ACCT_GRP --> ROLE_RES
    KEY_GRP --> ROLE_RES

    ROLE_RES --> PERM

    PERM -->|allowed| ALLOW[AuthzResult: allowed=true]
    PERM -->|denied| DENY[AuthzResult: allowed=false]

    style ALLOW fill:#4a4,color:#fff
    style DENY fill:#f44,color:#fff
```

An identifier an IdP asserts, in a token's groups claim, `X-Oidc-Groups` or a SCIM record, takes a group only when that group's `source_id` is the identifier and its `source_provider` is empty or the provider the login came through (or one `auth.source_provider_bindings` joins to it). A group's name links nothing, so an IdP group that happens to be called `dfe-admins` gets no roles until an admin links it. Logins through `X-Oidc-Groups` count as the provider named in `auth.proxy_provider` (default `oidc`).

For a local account the group files are the authority. The account's own `groups` list is kept in step by the API routes, but it grants nothing. A member removed from a group file in the live store loses that group's roles and orgs at its next engine request, whether the API removed it, someone edited the file in the engine's auth directory, or the Helm chart's `authConfig.groupsConfigMap` copied a new file in at pod start. The deploy repo's `governance/rbac` classes are neither read nor written at runtime, so an edit there changes nothing. An account an IdP owns (JIT or SCIM) also holds the groups its record says the IdP asserts, so a group an operator adds it to by hand sits beside those.

dfe-hyperdx decides on the `role` claim of the token it holds, so there a role taken away lasts until that token expires or is refreshed (`api.jwt_expire_minutes`).

### 2.2 Role Definitions

Roles are defined in `src/dfe_engine/auth/resources/roles.yaml` (built-in,
shipped with the package). Custom roles can be loaded from a separate YAML
file via `RoleConfig.load(path)`.

7 built-in roles, flat hierarchy (no inheritance):

```mermaid
graph LR
    subgraph "Global Roles"
        ADMIN["admin<br/>permissions: *"]
        DA["data_analyst<br/>hunt, query, source,<br/>fieldmap, alert,<br/>schema:read, transforms"]
        DAV["data_analyst_viewer<br/>read-only subset of<br/>data_analyst"]
        DV["data_viewer<br/>query:execute,<br/>source:read,<br/>dashboard:read"]
        IA["infra_admin<br/>config, service,<br/>helm, deployment,<br/>argo"]
        IV["infra_viewer<br/>read-only subset of<br/>infra_admin"]
    end

    subgraph "Scoped Roles"
        CV["org_viewer<br/>org-restricted<br/>data viewer<br/>(scoped: true)"]
    end

    style ADMIN fill:#c33,color:#fff
    style CV fill:#36c,color:#fff
```

### 2.3 Permission Taxonomy

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

### 2.4 Wildcard Permission Matching

```python
def permission_matches(permission: str, action: str) -> bool:
```

| Pattern | Matches | Does NOT match |
|---------|---------|----------------|
| `*` | Everything | — |
| `config:*` | `config:read`, `config:write`, `config:read:sub` | `hunt:read` |
| `service:*:config:*` | `service:loader:config:read` | `service:loader:metrics:read` |
| `service:*:config:read` | `service:loader:config:read` | `service:loader:config:write` |

- `*` alone matches ANY action regardless of segment count
- Trailing `*` matches any remaining segments
- Mid-position `*` matches exactly one segment

### 2.5 Authorization Engine

```python
# engine.py
def authorize(
    auth: AuthContext | None,
    action: str,
    resource: str = "",
    enabled: bool = True,
    role_config: RoleConfig | None = None,
) -> AuthzResult:
```

Decision order:
1. `enabled=False` → allow (dev/test)
2. `auth=None` → root mode, allow
3. Check `role_config.check_roles(auth.roles, action)` → first granting role
4. Return `AuthzResult(allowed, reason)`

### 2.6 RBAC Enforcement in API

```python
# api/deps.py
@router.post("/sources")
async def create_source(
    user: CurrentUser,
    _auth: None = Depends(require_action("source:write")),
): ...
```

`require_action(action)` calls `authorize()` and raises `AuthorizationError` on denial,
which the error handler converts to HTTP 403.

---

## 3. Account, Group, and API Key Stores

### 3.1 Store Architecture

```mermaid
classDiagram
    class AccountStore {
        +create(username, password, groups) Account
        +get(username) Account | None
        +list() list~Account~
        +update(username, **fields) Account
        +reset_password(username, new_password)
        +delete(username)
        +verify_password(username, password) bool
    }

    class GroupStore {
        +create(name, roles, description) Group
        +get(name) Group | None
        +list() list~Group~
        +update(name, **fields) Group
        +delete(name)
        +add_member(group_name, username)
        +remove_member(group_name, username)
        +resolve_roles_for_member(username) list~str~
    }

    class APIKeyStore {
        +create(name, groups, description) tuple~APIKey, str~
        +get(name) APIKey | None
        +list() list~APIKey~
        +verify(submitted_key) APIKey | None
        +revoke(short_token)
    }

    class LocalAuthProvider {
        +authenticate(username, password, ...) AuthContext
    }

    LocalAuthProvider --> AccountStore : verify password
    LocalAuthProvider --> GroupStore : resolve roles
    AccountStore ..> GroupStore : groups field references
```

All stores are YAML-backed (one file per entity, filename = identity).

### 3.2 File Formats

```yaml
# config/auth/accounts/analyst1.yaml
enabled: true
password_hash: "$2b$12$LJ3m..."
groups: ["soc-analysts"]        # kept in step with the group files; grants nothing for a local account
created_at: "2026-03-31T02:00:00Z"
updated_at: "2026-03-31T02:00:00Z"
```

```yaml
# config/auth/groups/soc-analysts.yaml
description: "SOC analyst team"
roles: ["data_analyst"]
members: ["analyst1", "analyst2"]
source_provider: ""              # OIDC provider the group is linked to (see 4.4)
source_id: ""                    # Provider-specific group ID
```

```yaml
# config/auth/api-keys/ci-deploy.yaml
enabled: true
short_token: "8a3f2c91"
key_hash: "sha384:38b060a7..."
groups: ["infra-ops"]
description: "CI/CD pipeline deployer"
created_at: "2026-03-31T02:00:00Z"
```

### 3.3 REST API

```
POST   /api/v1/auth/login                           # JWT login
POST   /api/v1/auth/refresh                          # Refresh JWT
GET    /api/v1/auth/me                               # Current user info + permissions
GET    /api/v1/auth/permissions                      # Current user's resolved permissions
```

Account, group, and API key CRUD endpoints are available via the stores and
CLI. The auth router currently exposes login/refresh/me/permissions.

### 3.4 Bootstrap Defaults

On first startup (empty `config/auth/` directory), `bootstrap_auth()` seeds:

**Groups:**
- `dfe-admins` → roles: `[admin]`
- `dfe-analysts` → roles: `[data_analyst]`
- `dfe-viewers` → roles: `[data_viewer]`
- `dfe-infra` → roles: `[infra_admin]`

**Accounts:** `admin` and `breakglass`, both in `dfe-admins`. See 3.5.

---

### 3.5 First Login: the two seeded accounts

The deploy mints the credentials, the engine refuses defaults, the UI never holds
a password.

**`admin`** takes its password from `DFE_AUTH_LOCAL_ADMIN_PASSWORD`, filled by the deployment's own secret store, issued with a forced change at first login. Every boot reasserts it until the admin replaces it, and after that only on a rotation.

**`breakglass`** is the recovery admin, verified against a bcrypt hash committed
at `governance/settings/auth.yaml` in the deploy repo, so it survives losing the
engine, the UI and the secret store:

```yaml
breakglass:
  password_hash: $2b$12$...   # minted once, never the plaintext
  enabled: true               # false locks the account out
```

`DFE_AUTH_BREAKGLASS_PASSWORD` mints that hash on the first boot with none
committed and is ignored afterwards. Gitops off means no hash and no break-glass
account. Every attempt is audit-logged (`auth.breakglass.login`); with
`enabled: false` the login returns 403 naming the setting.

**No `changeme` outside dev.** The engine refuses to start on an unset or default
admin password unless `DFE_ENV` is a dev posture -- the predicate gitops
auto-merge gates on (`settings.is_dev_posture`), and the error names the variable
and the fix. A dev posture running the default reports `default_credentials: true` on setup-status, login and refresh, for the UI to banner and force a change. Each reads the admin account at the request, so the forced change clears it with no restart.

**The predicate other repos copy** is `auth.bootstrap.default_credentials_in_use`:
strip surrounding whitespace, then treat empty and `changeme` as the default. An
exact comparison reads the `"changeme\n"` a Secret or `.env` line delivers as a
minted password.

**Reading the minted password.** setup-status carries `deploy_kind` (`docker` |
`kubernetes` | `local`, from `DFE_DEPLOYMENT_TARGET` else scalo's runtime
detection) and `credential_fetch_command`: `make creds` for docker, `kubectl -n
<ns> get secret <name> -o jsonpath='{.data.<key>}' | base64 -d` for kubernetes,
the environment file for local. Both survive setup completion (#301) -- that is
when an operator has lost the password.

**Rotation.** `POST /auth/accounts/{admin}/rotate-password` writes through the
scalo secrets seam when `DFE_AUTH_LOCAL_ADMIN_PASSWORD_SECRET_PATH` is set, else
returns 501 with `context.store_command`. The engine never writes the password
into its own YAML store. The new password faces the two rules that gate startup
-- 12 characters minimum, never the default -- so a rotation cannot lock the
deployment out of its own next boot. Either is a 422 naming the field.

---

## 4. OIDC Provider Integration

### 4.1 Architecture

```mermaid
flowchart TD
    subgraph "OIDC Providers"
        GOOGLE["Google Workspace<br/>Admin SDK groups.list"]
        ENTRA["Microsoft Entra ID<br/>Graph API /groups"]
        OKTA["Okta<br/>(stub — use token_claim)"]
        GENERIC["Generic OIDC<br/>(no admin API)"]
    end

    subgraph "dfe-engine"
        REG["OIDCProviderRegistry<br/>(YAML-backed CRUD)"]
        SYNC["sync_provider()<br/>Async group enumeration"]
        ADAPT["Adapter Factory<br/>get_adapter(provider)"]
        GS["GroupStore<br/>Create/update groups<br/>with source_provider metadata"]
    end

    REG --> ADAPT
    ADAPT --> GOOGLE
    ADAPT --> ENTRA
    ADAPT --> OKTA
    ADAPT --> GENERIC
    SYNC --> ADAPT
    SYNC --> GS
    SYNC --> REG

    subgraph "Group Resolution Modes"
        MANUAL["manual<br/>Membership managed<br/>in dfe-engine only"]
        CLAIM["token_claim<br/>Groups from OIDC<br/>token claim at login"]
        API["api<br/>Groups fetched from<br/>provider API on schedule"]
    end
```

### 4.2 Provider Configuration

```yaml
# config/auth/oidc-providers/{name}.yaml
type: "google"           # generic | google | entra_id | okta
enabled: true
display_name: "Google Workspace"
issuer: "https://accounts.google.com"
client_id: "1234.apps.googleusercontent.com"   # not secret
client_secret_path: "oidc/google-workspace/client_secret"
groups:
  mode: "api"            # manual | token_claim | api
  sync_interval: 3600
  # Google-specific
  service_account_json_path: "oidc/google-workspace/groups_service_account_json"
  admin_email: "admin@example.com"
  domain: "example.com"
```

Config holds a secret PATH into the `DfeSecrets` seam, or the NAME of an env var
(`client_id_env`, `client_secret_env`, `service_account_json_env`) — never a
secret value. Resolution reads the store first, so a credential sent to
`POST /api/v1/auth/oidc-providers` takes effect without a restart, then the
environment, so an ESO-mounted variable keeps working.

### 4.3 Adapter Implementations

| Adapter | Status | Admin API | Group sync |
|---------|--------|-----------|------------|
| Generic | Done | None | No (use `token_claim` mode) |
| Google | Done | Admin SDK `groups().list()` | Yes — full pagination |
| Entra ID | Done | Graph API `/groups` | Yes — `$top=999` pagination |
| Okta | Stub | Not implemented | No (Okta natively includes groups in ID token) |

All adapters are failsafe — credential or API failures return empty results
rather than raising exceptions, so auth continues working even if group
resolution degrades.

### 4.4 Group Sync Process

```mermaid
sequenceDiagram
    participant Sync as sync_provider()
    participant Reg as OIDCProviderRegistry
    participant Adapter as OIDC Adapter
    participant GS as GroupStore

    Sync->>Reg: get(provider_name)
    Reg-->>Sync: OIDCProvider config
    Note over Sync: Skip if disabled or mode != api

    Sync->>Adapter: list_all_groups()
    Adapter-->>Sync: list[GroupInfo]

    loop Each remote group
        Sync->>GS: get(group_name)
        alt No group of that name
            Sync->>GS: create(empty roles,<br/>source_provider + source_id)
        else Group linked to this IdP group
            Sync->>GS: update(metadata only,<br/>preserve existing roles)
        else Group not linked to it
            Note over Sync: skip, count name_taken
        end
    end

    Sync->>Reg: update(last_sync_at,<br/>last_sync_status)
```

The sync updates a stored group only when it is already linked to that IdP group: its `source_id` is the IdP group's id. The directory assigns that id, so a user cannot choose it the way they choose a display name. A group the sync creates is linked in the same write. A linked group keeps its roles, and the sync refreshes only its description and source metadata. Admins assign roles to synced groups by hand.

A name match links nothing. Directory display names are not unique, and in a tenant where users can create groups anyone can pick one, so an IdP group named `DFE Admins` would otherwise take the seeded `dfe-admins` and its `admin` role. A stored group of the same name with no `source_id`, or carrying another IdP group's id, is left untouched and counted as `name_taken`.

Linking an IdP group to the stored group that holds its name is an admin's act. Set `source_id` to the IdP group's id in that group's file, in the live store or through the chart's `authConfig.groupsConfigMap`; the next sync fills in `source_provider`. The groups API sets neither field. `PUT /api/v1/scim/v2/Groups/{name}` sets `source_id` from `externalId`, which links the group for logins and for the sync alike.

One bad group never aborts the sync. A provider group whose email, name or id makes no valid group name, whose name is held by a stored group that does not load, or whose name is held by a stored group not linked to it, is skipped and counted on `auth_oidc_sync_groups_skipped_total{reason}` (`invalid_name`, `stored_unloadable`, `name_taken`), and the provider's `last_sync_status` reads `partial` with each reason.

---

## 5. ClickHouse Tenant Isolation

### 5.1 Pinned Org Users, One Shared Policy Set

Every registered org gets its own ClickHouse user, and that user is what fences it in. The reconciler in `governance/ch/` renders the whole model from the org registry and the tier catalogue. The role, setting and user names come from the dfe-schemas catalogue, not from engine code.

- **One shared tenant role.** Every org-pinned user holds it and nothing else does.
- **One RESTRICTIVE row policy per table that carries `_org_id`**, on that role. Its predicate is `has(splitByChar(',', getSetting('<tenant setting>')), _org_id)`.
- **The tenant setting, pinned READONLY on each org user**, listing that org's `_org_id` values. A `SETTINGS` clause in query text cannot override it: ClickHouse answers 452 (SETTING_CONSTRAINT_VIOLATION).

A platform identity does not hold the tenant role, so no policy targets it and it reads every row. RESTRICTIVE-only is deliberate. A PERMISSIVE policy would flip the table to default-deny for everyone. A holder of the tenant role with no pinned setting makes `getSetting` throw, so a bad grant fails closed.

A tenant-reachable table with no `_org_id` column gets a `USING 0` policy, so a tenant reads nothing from it. The tables are found in `system.columns`.

Adding an org adds one pinned user. The policy set does not change.

### 5.2 Who Reads As Whom

```mermaid
flowchart TD
    CALLER["Caller's scoped grants"] --> PLAT{"platform_grants:<br/>a SYSTEM-scope role<br/>other than org_viewer?"}
    PLAT -->|"yes, with query:execute"| READER["Platform reader<br/>no tenant role, reads every org"]
    PLAT -->|no| ORG{"Resolves to exactly<br/>one registered org?"}
    ORG -->|yes| PINNED["That org's pinned user<br/>tenant role + pinned setting"]
    ORG -->|no| DENY["403 no_single_org<br/>fails closed"]
```

`platform_grants` (`auth/models.py`) is the one filter for "every org". The ClickHouse group bindings, the HyperDX connection read, the fork's role claim and JIT team assignment all go through it. A role bound at an org's scope covers that org alone, and `org_viewer` never unfences anyone.

Group bindings (`governance/ch/bindings.py`) follow the same rule. An org-scoped group binds to its org's pinned user whatever roles it holds. A system group holding a platform role reads unrestricted, and an `admin` or `infra_admin` group also reads the otel database. A group that claims an unregistered org gets no ClickHouse user at all, because the alternative is an unrestricted one.

### 5.3 The Engine's Own Reads

`connections.yaml` names the engine's own ClickHouse connections, and the engine asks for them by name. Only `default` is asked for, by schema discovery. The query API does not lean on a ClickHouse user for tenancy: the executor injects the caller's `org_id` as a reserved bind parameter that a client cannot override (`query/executor.py`, `RESERVED_PARAMS`).

---

## 6. Org Lifecycle

### 6.1 Org Registry

YAML-backed CRUD via `OrgRegistry`. One file per org in `config/orgs/`.

### 6.2 Org Creation Flow

```mermaid
sequenceDiagram
    participant API as REST API
    participant OR as OrgRegistry
    participant HDX as HyperDXClient

    API->>OR: create(name, org_ids, display_name)
    OR-->>API: Org YAML created

    Note over API: The org's pinned CH user arrives on the<br/>next CH RBAC reconcile, not here

    API->>HDX: get_team()
    HDX-->>API: team_id (or None on failure)

    Note over API: The team's ClickHouse connection is the<br/>fork's to create, from GET /api/v1/hyperdx/connection

    Note over HDX: HyperDX failures are non-fatal<br/>Background retry reconciliation
```

Creating an org does not create its ClickHouse user. The CH RBAC reconcile does, and it runs at engine startup and on `POST /api/v1/governance/ch-rbac/reconcile` (the console's "Reconcile Clickhouse RBAC" drawer). Until one of those runs, the org's users get `503 org_unprovisioned` from `GET /api/v1/hyperdx/connection`. The row policies need no change for a new org.

---

## 7. HyperDX Integration

### 7.1 Strategy

- **Connections:** the fork asks `GET /api/v1/hyperdx/connection` with the user's own token and seeds that one per-org credential on their team; the engine writes none itself
- **Runtime:** `HyperDXClient` calls the HyperDX internal API for the team and for per-DFE-source CRUD
- **Failures:** Non-fatal. First failure sets `_connected=False`, subsequent calls logged as warnings

### 7.2 Teams and Connections

- **Team name.** JIT picks it from the caller's grants (`auth/jit.py`, `resolve_hyperdx_team`): `dfe-admin` for `admin` or `infra_admin` at system scope, `dfe-analysts` for `data_analyst` at system scope, else `customer-<org>` for the caller's first org, else none.
- **Connection.** A team's ClickHouse connection is whatever `GET /api/v1/hyperdx/connection` returned to the user whose token seeded it (section 5.2): the platform reader, or that user's org's pinned user.

---

## 8. Argo CD Permissions

Roles carry `argo:{resource}:{action}` permissions in `roles.yaml`, and the scope catalogue publishes the `argo:` prefix (`auth/rbac_scopes`). The engine does not export them: it writes no `argocd-rbac-cm` policy and no AppProject roles.

---

## 9. Audit Logging

Every authorisation decision is logged for compliance (SOC 2, GDPR) via
structured OTel log events (`scalo.logger`).

| Event | Log key | Level |
|-------|---------|-------|
| Login success | `auth.login.success` | info |
| Login denied | `auth.login.denied` | warning |
| Permission denied | `auth.permission.denied` | warning |
| Account change | `auth.account.{change}` | info |
| Group change | `auth.group.{change}` | info |
| API key change | `auth.api_key.{change}` | info |

Events flow through the OTel pipeline → JSON → ClickHouse → HyperDX.
No custom ClickHouse audit table — standard OTel log ingestion is used.

---

## 10. AuthContext Model

```python
class AuthContext(BaseModel):
    org_id: str = "default"  # Primary tenant
    user_id: str  # Required unique identifier
    roles: list[str]  # Resolved DFE roles
    groups: list[str] = []  # OIDC or local groups
    org_ids: list[str] = []  # For customer-scoped roles
    connection_id: str = ""  # Resolved CH connection name
    request_id: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
```

Permissions are resolved at authorisation time from roles — never stored on
the context.

---

## 11. Settings

```python
class AuthSettings(BaseModel):
    enabled: bool = False  # Off by default (dev/test)
    auth_dir: str = ""  # Path to config/auth/ directory

    oidc: OIDCSettings  # Nested OIDC config


class OIDCSettings(BaseModel):
    providers_dir: str = ""  # Path to OIDC provider config dir
    sync_enabled: bool = True  # Enable background group sync
    sync_on_startup: bool = True  # Sync providers at startup
```

Environment variables: `DFE_AUTH_ENABLED`, `DFE_AUTH_DIR`,
`DFE_AUTH_OIDC_PROVIDERS_DIR`, `DFE_AUTH_OIDC_SYNC_ENABLED`,
`DFE_AUTH_OIDC_SYNC_ON_STARTUP`.

---

## 12. Module Map

```mermaid
graph TD
    subgraph "auth/"
        ENGINE["engine.py<br/>authorize()"]
        MODELS["models.py<br/>AuthContext, AuthzResult"]
        ROLES_MOD["roles.py<br/>RoleConfig, permission_matches()"]
        ACCOUNTS["accounts.py<br/>AccountStore"]
        GROUPS["groups.py<br/>GroupStore"]
        APIKEYS["api_keys.py<br/>APIKeyStore"]
        LOCAL["local_provider.py<br/>LocalAuthProvider"]
        BOOT["bootstrap.py<br/>bootstrap_auth()"]
        AUDIT["audit.py<br/>audit_*() functions"]

        subgraph "oidc/"
            OIDC_REG["registry.py<br/>OIDCProviderRegistry"]
            OIDC_SYNC["sync.py<br/>sync_provider()"]
            OIDC_MODELS["models.py<br/>OIDCProvider, GroupInfo"]
            subgraph "adapters/"
                GENERIC_A["generic.py"]
                GOOGLE_A["google.py"]
                ENTRA_A["entra.py"]
                OKTA_A["okta.py (stub)"]
            end
        end

        subgraph "resources/"
            ROLES_YAML["roles.yaml<br/>7 built-in roles"]
        end
    end

    subgraph "api/"
        DEPS["deps.py<br/>get_current_user()<br/>require_action()"]
        AUTH_ROUTER["v1/auth.py<br/>login, refresh, me"]
        APP["app.py<br/>lifespan bootstrap"]
    end

    subgraph "connections/"
        CONN_REG["registry.py<br/>ConnectionRegistry"]
    end

    subgraph "governance/ch/"
        CH_RBAC["reconciler.py<br/>tenant role, row policies,<br/>pinned org users"]
    end

    subgraph "orgs/"
        ORG_REG["registry.py<br/>OrgRegistry"]
    end

    subgraph "hyperdx/"
        HDX["client.py<br/>HyperDXClient"]
    end

    DEPS --> ENGINE
    DEPS --> ACCOUNTS
    DEPS --> GROUPS
    DEPS --> APIKEYS
    AUTH_ROUTER --> LOCAL
    LOCAL --> ACCOUNTS
    LOCAL --> GROUPS
    ENGINE --> ROLES_MOD
    ROLES_MOD --> ROLES_YAML
    BOOT --> ACCOUNTS
    BOOT --> GROUPS
    OIDC_SYNC --> OIDC_REG
    OIDC_SYNC --> GROUPS
    CH_RBAC --> ORG_REG
    CH_RBAC --> GROUPS
```

---

## 13. Phase 4 status - schema-less service discovery

Adding a new `dfe-transform-elastic` should require zero Python code
changes. Shipped so far:

1. **Service surface YAML files** (`config/service-surfaces/{name}.yaml`)
   describing configurable settings and metrics - SHIPPED
   (`services/surfaces/registry.py`)
2. **`/api/v1/service-surfaces/`** API endpoints with RBAC - SHIPPED

Remaining:

3. **Metrics manifest caching** from the scalo-rs `/metrics/manifest`
   endpoint
4. **Removal of the typed plugin system** (`plugins.py`,
   `plugins_builtin/` - still present, still the live path)

---

## 14. Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Multi-role user | Highest-privilege connection wins (precedence order) |
| HyperDX unavailable | Non-fatal. Org CRUD succeeds. Sync retried in background |
| ClickHouse unavailable | Degraded mode: API serves cached state, `/readyz` → 503, retry every 60s |
| Unknown OIDC group | No matching group file → no roles resolved → default deny |
| OIDC adapter failure | Failsafe: returns empty results, auth continues with available info |
| Default password in use | Warning logged at startup |

---

## 15. Non-Goals

- Per-metric RBAC granularity (access is per-service, not per-metric)
- Per-setting RBAC granularity (access is per-service config, not per-key)
- HyperDX per-user RBAC (solved at dfe-engine layer via teams)
- Dynamic K8s service discovery (future enhancement)
- Envoy Gateway SecurityPolicy CRD generation (managed by dfe-infra)
- ClickHouse cluster provisioning (managed by dfe-infra)
- Role hierarchy / inheritance (flat roles sufficient for 7-role set)

---

## 16. Breaking Changes from Pre-2.2

| What | Old | New |
|------|-----|-----|
| JWT library | `python-jose[cryptography]` | `PyJWT[crypto]` |
| Auth model | `AuthContext.permissions` field | Removed — resolved from roles at auth time |
| Role names | `admin`, `operator`, `viewer` | 7 granular roles in `roles.yaml` |
| Role storage | `DEFAULT_ROLE_PERMISSIONS` constant | `auth/resources/roles.yaml` |
| Account storage | Hardcoded dict in `LocalAuthProvider` | `config/auth/accounts/*.yaml` with full CRUD |
| Group mapping | `AuthSettings.group_role_mapping` | `config/auth/groups/*.yaml` |
| Auth paths | JWT Bearer only | OIDC headers + API key + JWT Bearer + disabled |
| CH connections | Single global client | `ConnectionRegistry` (multi-client, tenant-scoped) |
| Deep merge | `deepmerge` pip package | `dfe_engine.yaml_utils.deep_merge()` (vendored) |

---

## 17. References

- [ClickHouse Custom Settings + Row Policy (Highlight)](https://www.highlight.io/blog/row-level-security)
- [API Key Prefix Pattern (Seam)](https://github.com/seamapi/prefixed-api-key)
- [Multi-Tenant RBAC Design (WorkOS)](https://workos.com/blog/how-to-design-multi-tenant-rbac-saas)
- [Envoy Gateway OIDC SecurityPolicy](https://gateway.envoyproxy.io/docs/tasks/security/oidc/)
