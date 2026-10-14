# RBAC, Multi-Tenant ClickHouse, and Auth Architecture

**Scope:** Auth, granular RBAC, multi-tenant CH with row-level security, OIDC group sync, HyperDX integration, Argo CD RBAC export

---

## Implementation Status

| Phase | Scope | Status |
|-------|-------|--------|
| **Phase 1** | RBAC foundation, account/group/API key CRUD, 4 auth paths, audit | Done |
| **Phase 2** | Per-org pinned ClickHouse users, shared tenant row policies, quota tiers | Done |
| **Phase 3** | OrgRegistry, HyperDX team/connection sync | Done |
| **Phase 4** | Schema-less service discovery | Not pursued - see section 13 |

---

## 1. Auth Flow

### 1.1 Four Authentication Paths

```mermaid
flowchart TD
    REQ[Incoming Request] --> OIDC{X-Oidc-Subject header<br/>and auth.trust_proxy_auth_headers?}
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

    JWT -->|No| DISABLED{auth.enabled = false<br/>and a dev DFE_ENV?}
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
| API key | CI/CD, Terraform, scripts | `config/auth/api-keys/*.yaml` (SHA-384 hash) | `expires_at` if set, else `auth.api_key_default_ttl_days` from creation (default 90, `0` for no expiry). Revoke by deleting the file |
| JWT Bearer | Console sessions, standalone, dev | Issued by `POST /api/v1/auth/login`, the OIDC login callback and `POST /api/v1/auth/refresh` | `api.jwt_expire_minutes` (`DFE_API_JWT_EXPIRE_MINUTES`, default 60) |
| Disabled | Dev/test | N/A | N/A |

A JWT carries identity, not authority. Its `roles`, `groups` and `org_ids` claims grant nothing: every request re-resolves them from the account the `sub` binds to, so a role or org taken away is gone from the next request. A `sub` that binds no account holds nothing -- an API-key subject, a deleted account -- and with auth enabled `POST /api/v1/auth/refresh` answers 401 for it, since each refresh issues a new expiry.

OIDC headers injected by Envoy Gateway SecurityPolicy, read only when `auth.trust_proxy_auth_headers` (`DFE_AUTH_TRUST_PROXY_AUTH_HEADERS`) is true:

| Header | Content |
|--------|---------|
| `X-Oidc-Subject` | User email or unique ID |
| `X-Oidc-Email` | User email, optional |
| `X-Oidc-Groups` | Comma-separated OIDC group identifiers |

### 1.2 Deployment Modes

Local auth is **always available**. OIDC headers are additive -- checked
first when present and `auth.trust_proxy_auth_headers` is true, but JWT and
API key paths remain active. With the flag off (the default) the engine ignores
the headers and falls through to API key and JWT.

| Mode | Setting | What's active | Use case |
|------|---------|--------------|----------|
| **Dev/test** | `auth.enabled=false` with a dev `DFE_ENV` | Requests with no credential get root admin context. Any other `DFE_ENV` refuses to start | Local development |
| **Standalone** | `auth.enabled=true` (the default) | JWT Bearer + API keys + local accounts | Docker, no external IdP |
| **Production** | `auth.enabled=true` + `auth.trust_proxy_auth_headers=true` + Envoy | OIDC headers (precedence) + JWT + API keys | K8s with Envoy Gateway |

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
| Role definitions | `rbac/roles.yaml` beside the auth directory, seeded from `auth/resources/roles.yaml` on first boot | Permission patterns | Yes |
| Group→role mapping | `groups/{name}.yaml` | Role list | Yes |

#### Config Directory Layout

```text
config/auth/
    accounts/           # One YAML per local user account
        admin.yaml
        analyst1.yaml
    groups/              # One YAML per group (group → roles)
        dfe-admins.yaml
        soc-analysts.yaml
    api-keys/            # One YAML per API key
        ci-deploy.yaml
    oidc-providers/      # One YAML per OIDC provider (see 4.2)
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

```text
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
  file stays until revoked, and `list` reports `expired: true`. A key created
  through the API without one expires after `auth.api_key_default_ttl_days`
  (default 90, `0` for no expiry)
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
        OIDC_GRP["OIDC: X-Oidc-Groups, recorded on the<br/>account the sign-in binds"]
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

    OIDC_GRP --> ACCT_GRP
    ACCT_GRP --> ROLE_RES
    KEY_GRP --> ROLE_RES

    ROLE_RES --> PERM

    PERM -->|allowed| ALLOW[AuthzResult: allowed=true]
    PERM -->|denied| DENY[AuthzResult: allowed=false]

    style ALLOW fill:#4a4,color:#fff
    style DENY fill:#f44,color:#fff
```

An identifier an IdP asserts, in a token's groups claim, `X-Oidc-Groups` or a SCIM record, takes a group only when that group's `source_id` is the identifier and its `source_provider` is the provider the login came through (or one `auth.source_provider_bindings` joins to it). A group's name links nothing, so an IdP group that happens to be called `dfe-admins` gets no roles until an admin links it. Logins through `X-Oidc-Groups` count as the provider named in `auth.proxy_provider` (default `oidc`).

A link names its provider. A group with a `source_id` and no `source_provider` would answer every provider that asserts the id, so the group stores refuse to write one. A group file or document already carrying one loads unlinked: the id is dropped and links no provider, its members keep the group's roles, and the engine logs a warning naming the group and counts it on `auth_groups_unlinked_total{reason="no_provider"}`. The next write to that group saves it without the id, so link it again by setting both fields.

`auth.source_provider_bindings` joins each stamp straight to one OIDC provider, and settings refuse a chain such as `{"legacy": "scim", "scim": "entra"}`, where a target is itself a stamp. An IdP sign-in is admitted on the providers its account's own stamp answers for, the set its roles resolve through, so a SCIM account stamped `scim` is admitted on `scim` and `entra` but not on another stamp bound to `entra`.

A sign-in through `X-Oidc-Groups` resolves its roles the way a token does, from the account the sign-in binds: the groups its asserted identifiers link to, plus every group whose members list the account. A gateway that forwards no groups claim, Envoy among them, reaches roles only through that membership, so on those deployments adding an account to a group by hand is how its roles arrive.

The linked groups are also the sign-in allowlist. An IdP sign-in, through the OIDC callback or `X-Oidc-Groups`, is admitted only when the account it binds to would hold a role, from a group its identifiers link to or a group whose members name the account, so a linked group with no role (every group the sync creates, until an admin gives it one) admits nobody. Anyone else gets `401 no_role_granted` (a sign-in the console started, with `return_to`, goes back to the console with that code in the URL fragment instead) and an `auth.jit.login_refused` audit event, a first sign-in leaves no account, and an existing account records the groups its IdP now asserts and has every session ended, as a logout ends them, so an issued token answers `401 session_ended` on any route, refresh included. The local admin or the break-glass account (section 3.5) links the first group, and adding an account to a group by hand admits someone the IdP groups do not cover, which is the only way in through a `manual`-mode provider.

An admin sets `source_id` and `source_provider` together through the groups API or dfe-ui's group form (section 4.4); `source_provider` names the provider whose logins the link answers. Where a `token_claim` provider's claim carries names (Okta's default, dex, Keycloak), `source_id` is the name the IdP sends; for Entra it is the object ID.

A groups claim sent as one string is split on commas (on whitespace when it has none, `auth/oidc/rp.py`) and `X-Oidc-Groups` is split on commas. So a group name containing a space needs an array claim; a `source_id` containing a comma matches only an array claim on the relying-party path.

For a local account the group files are the authority. The account's own `groups` list is kept in step by the API routes, but it grants nothing. A member removed from a group file in the live store loses that group's roles and orgs at its next engine request, whether the API removed it, someone edited the file in the engine's auth directory, or the Helm chart's `authConfig.groupsConfigMap` copied a new file in at pod start. The deploy repo's `governance/rbac` classes are neither read nor written at runtime, so an edit there changes nothing. An account an IdP owns (JIT or SCIM) also holds the groups its record says the IdP asserts, so a group an operator adds it to by hand sits beside those.

dfe-hyperdx decides on the `role` claim of the token it holds, so there a role taken away lasts until that token expires or is refreshed (`api.jwt_expire_minutes`).

### 2.2 Role Definitions

The built-in roles ship in `src/dfe_engine/auth/resources/roles.yaml`. On first
boot the engine copies that file to `rbac/roles.yaml` beside the auth directory
and loads roles from the copy. Custom roles are added to it through
`/api/v1/auth/roles`.

The copy is never rewritten once it exists, and the API refuses edits to a built-in role. A built-in grant changed in a later engine release therefore reaches an existing deployment only when `rbac/roles.yaml` is edited by hand.

8 built-in roles, flat hierarchy (no inheritance):

```mermaid
graph LR
    subgraph "Global Roles"
        ADMIN["admin<br/>permissions: *"]
        DA["data_analyst<br/>hunt, query, source,<br/>fieldmap, alert, rule,<br/>schema, transform"]
        DAV["data_analyst_viewer<br/>read-only subset of<br/>data_analyst"]
        DV["data_viewer<br/>query:execute,<br/>source:read,<br/>dashboard:read"]
        IA["infra_admin<br/>config, service,<br/>helm, deployment,<br/>argo"]
        IV["infra_viewer<br/>read-only subset of<br/>infra_admin"]
        OP["dfe_operator<br/>invoke shipped actions,<br/>read governed state"]
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
| `hunt` | `read`, `write`, `delete`, `execute`, `*` | No |
| `query` | `read`, `execute`, `*` | No |
| `source` | `read`, `write`, `delete`, `deploy`, `*` | No |
| `fieldmap` | `read`, `write`, `delete`, `*` | No |
| `alert` | `read`, `write`, `delete`, `*` | No |
| `schema` | `read`, `write`, `delete`, `*` | No |
| `dashboard` | `read`, `*` | No |
| `config` | `read`, `write`, `*` | No |
| `transform` | `compile`, `test`, `*` | No |
| `service` | `read`, `write`, `delete`, `validate`, and per service `config:read`, `config:write`, `metrics:read`, `*` | The `config` and `metrics` actions (`service:{name}:{action}`) |
| `helm` | `compile`, `execute_ddl`, `create_topics`, `*` | No |
| `deployment` | `read`, `write`, `*` | No |
| `org` | `read`, `write`, `*` | No |
| `argo` | `{resource}:{action}` (open-ended) | No |

The routes enforce more domains than this table lists (accounts, groups, API keys, roles, OIDC providers, governance, and others). The full catalogue is `src/dfe_engine/auth/rbac_scopes/scope_constants.py`.

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
    *,
    scope: Scope | None = None,
    enabled: bool = True,
    role_config: RoleConfig | None = None,
) -> AuthzResult:
```

Decision order:

1. `enabled=False` → allow (dev/test)
2. `auth=None` → root mode, allow
3. For each of the caller's scoped grants (`auth.grants`, else `auth.roles` as system grants): allow when the grant's scope covers the requested scope and its role's permissions match the action. `scope=None` is a system-scope check
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
source_provider: ""              # identity provider the group is linked to (see 4.4)
source_id: ""                    # that provider's group ID; needs source_provider
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

```text
POST   /api/v1/auth/login                           # JWT login
POST   /api/v1/auth/refresh                          # Refresh JWT
POST   /api/v1/auth/logout                           # End the session
GET    /api/v1/auth/me                               # Current user info + permissions
GET    /api/v1/auth/permissions                      # Current user's resolved permissions
GET    /api/v1/auth/setup-status                     # First-login state (see 3.5)
POST   /api/v1/auth/setup/retire-admin               # Retire the bootstrap admin
GET    /api/v1/auth/oidc/{provider}/login            # OIDC sign-in (see 4)
GET    /api/v1/auth/oidc/{provider}/callback         # OIDC callback, mints the engine JWT
```

Account, group, API key, role and OIDC provider CRUD sit under
`/api/v1/auth/accounts`, `/api/v1/auth/groups`, `/api/v1/auth/api-keys`,
`/api/v1/auth/roles` and `/api/v1/auth/oidc-providers`. `openapi-spec/openapi.json`
lists every route.

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

**Changing either password.** `POST /auth/accounts/{username}/reset-password` works for both accounts on every deployment, and is the console's one password action for each. The new password works at the next sign-in, and the engine keeps it where that account's password lives so a restart does not undo it:

| Account | Kept in | Gitops off |
|---|---|---|
| `admin` | the live account store, and its whole account doc at `governance/rbac/accounts/<admin>.yaml` | the live account store only |
| `breakglass` | its bcrypt hash at `governance/settings/auth.yaml`, which every boot seeds the account from | no `breakglass` account |

A deployment that commits straight to main (dev, solo, auto-merge) is done at once. Production with `DFE_GITOPS_MODE=team` opens a review instead, and the response's `git.pending` carries the PR URL, or the merge command when no forge is configured. A restart before the review merges brings the old password back. `GET /auth/accounts/{username}/git-status` reports when it has merged.

**Rotation.** `POST /auth/accounts/{admin}/rotate-password` changes the value the deployment injects, not the live password: it writes the deployment's secret store when `DFE_AUTH_LOCAL_ADMIN_PASSWORD_SECRET_PATH` is set, else returns 501 with the command that changes it where the deployment keeps it (`context.store_command`). The next boot issues the rotated value with a forced change. The engine never writes the password into its own YAML store. The new password faces the two rules that gate startup -- 12 characters minimum, never the default -- so a rotation cannot lock the deployment out of its own next boot. Either is a 422 naming the field. Any other account answers 400 `not_store_backed` and is reset instead.

**The admin is kept.** The console never retires `admin`: its first login replaces the minted password, and from then on it is reset like any other account. `POST /api/v1/auth/setup/retire-admin` remains for an operator whose policy is to disable it once the deployment has an admin of its own.

---

## 4. OIDC Provider Integration

### 4.1 Architecture

```mermaid
flowchart TD
    subgraph "OIDC Providers"
        GOOGLE["Google Workspace<br/>Cloud Identity as the user at login<br/>Admin SDK groups.list, optional"]
        ENTRA["Microsoft Entra ID<br/>groups claim, Graph on overage<br/>Graph API /groups for sync"]
        OKTA["Okta<br/>groups claim, or<br/>Groups API in api mode"]
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
        API["api<br/>Groups from the provider API:<br/>scheduled sync, and at login<br/>with enrich_on_login"]
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
  mode: "api"            # manual | token_claim | api (google: api only)
  sync_interval: 3600
  enrich_on_login: true  # required for google: its tokens carry no groups
  # Google, optional: a service account holding a groups admin role, for sync
  service_account_json_path: "oidc/google-workspace/groups_service_account_json"
  domain: "example.com"
```

A Google login reads the user's groups from Cloud Identity with that login's own token (scope `cloud-identity.groups.readonly`, Cloud Identity API enabled in the OAuth client's project). A group file links on `source_id`, the id after `groups/` in the group's resource name.

The customer's IdP admin sets up their side from [idp-setup/google.md](idp-setup/google.md), [idp-setup/entra.md](idp-setup/entra.md) or [idp-setup/okta.md](idp-setup/okta.md), and sends back the client credentials, the issuer and the group identifiers each provider links on.

Config holds a secret PATH into the `DfeSecrets` seam, or the NAME of an env var
(`client_id_env`, `client_secret_env`, `service_account_json_env`) — never a
secret value. Resolution reads the store first, so a credential sent to
`POST /api/v1/auth/oidc-providers` takes effect without a restart, then the
environment, so an ESO-mounted variable keeps working.

Surrounding whitespace, such as the newline a mounted secret file ends with, is stripped from a resolved credential. An Okta API token still holding a space, a control character or a non-ASCII one is never sent: the sync reads `error` and the connection test names the setting, never the value. Each secret's `*_env` field (`client_secret_env` plus `groups.api_token_env`, `groups.client_secret_env` and `groups.service_account_json_env`) moves that secret between the store and an env var on `PUT /api/v1/auth/oidc-providers/{name}`:

| Request sends | Result |
|---|---|
| no `*_env` field | the env var name and any stored copy are kept |
| `*_env` set to `""` | the name is cleared, the stored copy is kept |
| a name, with no value for the secret, that differs from the one held or sits beside a stored copy | the env var must resolve non-empty in the engine: the name is saved and the stored copy is deleted once the update is saved |
| the same, but the engine does not set that env var | `422`, the field error `env_var_not_set` on that `*_env` field; nothing is saved or deleted |
| the name already held, with nothing stored | no change |
| a value for the secret | the value is stored and the name saved as sent |

A create naming an env var the engine does not set, with no value for that secret, is refused the same way. A stored copy whose delete fails after the update is saved is logged by its store path; the update still answers 200.

### 4.3 Adapter Implementations

| Adapter | Status | Admin API | Group sync |
|---------|--------|-----------|------------|
| Generic | Done | None | No (use `token_claim` mode) |
| Google | Done | Cloud Identity as the user at login; Admin SDK `groups().list()` as an optional service account | Only with a service account |
| Entra ID | Done | Graph `/me/transitiveMemberOf` with the user's token on a >200 group overage, falling back to app-only `/users/{oid}/transitiveMemberOf` when Graph refuses that token. Graph `/groups` for sync | Yes, api mode with app credentials -- `$top=999` pagination |
| Okta | Done | Groups API `/api/v1/groups` with an API token, api mode only. `token_claim` calls nothing | Yes, api mode -- `Link` header pagination |

A sign-in's group lookup fails safe. A credential or API failure, or a lookup still paging after 50 pages, returns no groups, so sign-in keeps working while group resolution degrades. The group sync's listing does the opposite and raises, so a failed listing never reads as a directory with no groups (section 4.4).

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

    Sync->>GS: list()
    GS-->>Sync: stored groups, by their link

    loop Each remote group
        alt Group linked to this IdP group, through this provider
            Sync->>GS: update(description only,<br/>preserve existing roles)
        else Group of any name carrying its id, linked to another provider
            Note over Sync: skip, count provider_mismatch
        else No group of that name
            Sync->>GS: create(empty roles,<br/>source_provider + source_id)
        else Group not linked to it
            Note over Sync: skip, count name_taken
        end
    end

    Sync->>Reg: update(last_sync_at,<br/>last_sync_status)
```

The sync updates a stored group only when it is already linked to that IdP group: its `source_id` is the IdP group's id and its `source_provider` is this provider, or one `auth.source_provider_bindings` joins to it. The directory assigns that id, so a user cannot choose it the way they choose a display name. Okta is the exception: its groups claim carries names, so an Okta group's id here is its name ([idp-setup/okta.md](idp-setup/okta.md)). A group the sync creates is linked in the same write. A linked group keeps its roles, and the sync refreshes only its description. Admins assign roles to synced groups by hand.

A name match links nothing. Directory display names are not unique, and in a tenant where users can create groups anyone can pick one, so an IdP group named `DFE Admins` would otherwise take the seeded `dfe-admins` and its `admin` role. A stored group of the same name with no `source_id`, or carrying another IdP group's id, is left untouched and counted as `name_taken`. So is a new group whose name is a stored group's `source_id` or whose id is a stored group's name.

A sync never moves a group between providers. Two providers can assign one id, and Okta's ids are group names, so an Okta group named `SOC Analysts` carries the same id as a Keycloak group of that name. A stored group carrying the IdP group's id but linked to another provider, whatever it is called, is left untouched and counted as `provider_mismatch`, so the sync never creates a second group carrying the id, and the engine log names the syncing provider, the provider the group is linked to and the reason. `auth.source_provider_bindings` declares two names one identity source, either way round, so with `{"scim": "entra"}` the `entra` sync refreshes a group SCIM stamped `scim` and leaves the stamp as it is.

Linking an IdP group to a stored group is an admin's act. Set both `source_provider`, the provider's name in the registry, and `source_id`, the IdP group's id, through the groups API (`POST` or `PUT /api/v1/auth/groups`), dfe-ui's group form, the group's file in the live store or the chart's `authConfig.groupsConfigMap`. `PUT /api/v1/scim/v2/Groups/{name}` sets `source_id` from `externalId`. On a group not yet linked it also sets `source_provider` to `scim`, so an OIDC sign-in reaches the group only through the provider `auth.source_provider_bindings` binds to `scim`. A group already linked keeps its provider.

The groups API refuses a link no login could use to reach the group alone:

- `409 conflict`: another group carries the `source_id`, under any provider (SCIM answers `uniqueness`).
- `409 identifier_taken`: a new group's name is another group's `source_id` or a new or changed `source_id` is another group's name (SCIM answers `uniqueness` for a `displayName` or `externalId`). An API key resolves each group identifier by name, then by `source_id`, so an identifier in both namespaces would let a key minted for one group take the other's roles once the first is deleted. A group whose name is its own `source_id` is one group and is accepted. A pair already stored is logged as a warning at startup, naming both groups.
- `422 invalid_source_id`: the `source_id` is over 512 characters, starts or ends with whitespace or holds a control character.
- `422 invalid_source_provider`: the `source_provider` is over 512 characters or is not an OIDC provider, a binding, `scim` or (while `auth.trust_proxy_auth_headers` is on) `auth.proxy_provider`.
- `422 missing_source_provider`: a new or changed link has a `source_id` but no `source_provider`.

Only a changed value is checked, so an edit to other fields of a group whose stored link these rules now refuse still succeeds. Setting, changing or clearing a link needs the role rights adding a member does, since each changes who holds the group's roles. Deleting a linked group needs them too, through the groups API and SCIM alike.

An account write is checked against the groups the account holds before and after it. An IdP-owned account's own list is also its assertion, so a name in that list that is another group's `source_id` is checked as that group as well.

A group write, through the groups API or SCIM, changes only the group's member list. It updates a local account's own `groups` list to match but never an IdP-owned account's (one with `source_provider` set): that list is what the IdP asserts, so a group name written there would be read as an assertion. The member still holds the group through the member list, which SCIM `/Users` reports.

One bad group never aborts the sync. A provider group that carries no id, whose email, name or id makes no valid group name, whose name is held by a stored group that does not load, whose name is held by a stored group not linked to it, or whose stored group is linked to another provider, is skipped and counted on `auth_oidc_sync_groups_skipped_total{reason}` (`no_id`, `invalid_name`, `stored_unloadable`, `name_taken`, `provider_mismatch`), and the provider's `last_sync_status` reads `partial` with each reason. A group with no id can never be linked, from any provider, so the engine log counts those in one line per run and names none of them.

A listing that fails on any page, the first or the last, stops the sync before it writes a group. So does a page without the listing's shape, and a listing still handing out pages after 1,000. `last_sync_status` reads `error`, and `sync_error` says what failed: the provider's HTTP status, a page it could not read, or the page limit. A 429 reads as throttling, not as a refusal. The engine log carries the provider's error code, and the next run lists again from the start.

A credential that is set but gives no access fails the same way. Entra issuing no token for the client secret, a Google service account JSON that does not build a client, or Google's token endpoint refusing the service account (`invalid_grant`), reads `error`, and `sync_error` says the directory API could not use the configured credential. The engine log has the reason. An Entra that cannot be reached, throttles the token request (429) or fails it (5xx) says that instead, and never blames the credential. `GET /oidc-providers/{name}/test` reports an Entra credential the same way, and keeps "not configured" for one that was never set.

An api-mode Entra or Okta provider with no directory credential, or a Google provider whose configured service account resolves to nothing, has nothing to list with. Its `last_sync_status` reads `not_configured`, `sync_error` says no credential is configured, and `POST /oidc-providers/{name}/sync` answers with the same text in `skipped`. A Google provider with no service account reads `user_token` with an empty `sync_error`, because each sign-in reads that user's groups with their own token. A credential whose secret store does not answer reads `error`, and `sync_error` names the secret store. One whose secret store refuses the engine access (a 403 on the path, or a refused AppRole login) also reads `error`, and `sync_error` says the store refused access, so the fix is the engine's secrets policy rather than the store's reachability.

A `generic` provider has no directory API, so one set to api mode reads `error`, and `sync_error` says to set `groups.mode` to `token_claim`.

The sync answer's `status` field carries the outcome: `ok`, `partial`, `error`, `not_configured`, `user_token` or `skipped`. The scheduled sync counts each run on `auth_oidc_sync_scheduled_runs_total{outcome}`. A provider that is not configured is tried again after its `sync_interval`, so adding the credential is picked up without a restart.

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
    CALLER["Caller's scoped grants"] --> PLAT{"platform_grants:<br/>a SYSTEM-scope role,<br/>declared, not scoped,<br/>not org_viewer?"}
    PLAT -->|"yes, with query:execute"| READER["Platform reader<br/>no tenant role, reads every org"]
    PLAT -->|no| ORG{"Resolves to exactly<br/>one registered org?"}
    ORG -->|yes| PINNED["That org's pinned user<br/>tenant role + pinned setting"]
    ORG -->|no| DENY["403 no_single_org<br/>fails closed"]
```

`platform_grants` (`auth/models.py`) is the one filter for "every org". The ClickHouse group bindings, the sampler, the HyperDX connection read, the fork's role claim and JIT team assignment all go through it. A role bound at an org's scope covers that org alone, a role marked `scoped` in `roles.yaml` is held to its holder's orgs wherever it is bound, a role `roles.yaml` does not declare unfences nothing, and `org_viewer` never unfences anyone.

Group bindings (`governance/ch/bindings.py`) follow the same rule. An org-scoped group binds to its org's pinned user whatever roles it holds. A system group holding a platform role reads unrestricted, and an `admin` or `infra_admin` group also reads the otel database. A group's claimed org markers resolve to a registered org by name or, failing that, by the one org that declares the marker as a tenant id (`orgs/tenant_scope.py::resolve_orgs`); a marker no org declares, or a tenant id two orgs both declare, resolves to none, so the group gets no ClickHouse user at all -- the alternative is an unrestricted one.

### 5.3 The Engine's Own Reads

`connections.yaml` names the engine's own ClickHouse connections, and the engine asks for them by name. Only `default` is asked for, by schema discovery. The query API does not lean on a ClickHouse user for tenancy: the executor injects the caller's `org_id` as a reserved bind parameter that a client cannot override (`query/executor.py`, `RESERVED_PARAMS`).

The row reads the engine makes for a caller with no platform grant -- the sampler, the transform dry run, and the schema routes `sample-rows`, `json-paths` and `promote-field` -- bind `_org_id` to the tenant ids of the caller's registered orgs, the same ids each org's pinned user carries (`orgs/tenant_scope.py`). Each org marker the caller carries resolves the same way as a group's (`resolve_orgs`): by name, or failing that by the one org declaring it as a tenant id; a marker no org declares, or a tenant id two orgs share, binds nothing, and a caller left with no tenant id, or a table with no `_org_id`, is refused before any row is read. `/tasks` shows such a caller only the tasks held to its orgs.

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

Creating an org does not create its ClickHouse user. The CH RBAC reconcile does, and it runs at engine startup, in the background after every org or group change, and on `POST /api/v1/governance/ch-rbac/reconcile` (the console's "Reconcile Clickhouse RBAC" drawer). Until one of those runs, the org's users get `503 org_unprovisioned` from `GET /api/v1/hyperdx/connection`. The row policies need no change for a new org.

A reconcile that raises, ClickHouse unreachable being the usual cause, is logged with the cause and run again in the background on a jittered back-off from 5 seconds up to 5 minutes, until one gets through. That includes the startup run, so a ClickHouse that comes up after the engine still gets every service user the dfe-schemas role catalogue mints without a restart. With tenant isolation off only the service roles reconcile, and they retry the same way. A successful `POST` stands a waiting retry down. Each retry adds 1 to `ch_rbac_reconcile_retries_total` (`reconcile`: `rbac` or `service_roles`), and every run counts on `ch_rbac_reconciles_total` (`outcome`). None of it gates readiness.

`create` and `update` both refuse a name or tenant id that collides with another org's name or tenant ids -- `409 conflict` naming the other org -- because a shared marker would let their pinned ClickHouse users read each other's rows. An org keeping its own name among its own tenant ids is not a collision.

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

Logins, denied authorisation decisions and admin changes are logged for compliance (SOC 2, GDPR) via structured OTel log events (`scalo.logger`). An allowed decision is not logged.

| Event | Log key | Level |
|-------|---------|-------|
| Login success | `auth.login.success` | info |
| Login denied | `auth.login.denied` | warning |
| Break-glass login | `auth.breakglass.login` | warning |
| Permission denied | `auth.permission.denied` | warning |
| Account change | `auth.account.{change}` -- `created`, `updated`, `deleted`, `password_reset`, `password_changed` (the owner's own), `attributes_updated`, `sensitive_attributes_updated`, a password rotation, ending sessions, retiring the bootstrap admin | info |
| Group change | `auth.group.{change}` -- `created`, `updated`, `deleted`, `member_added`, `member_removed`, `attributes_updated`, `sensitive_attributes_updated` | info |
| API key change | `auth.api_key.{change}` -- `created`, `revoked` | info |
| Role change | `auth.role.{change}` -- `created`, `updated`, `deleted` | info |
| JIT provisioning | `auth.jit.{event}` -- account created, groups updated, login refused, team assigned, HyperDX invite, failure | info / warning |
| Org change | `org.{change}` | info |
| Resource change | `resource.{type}.{change}` -- OIDC providers, sources, fieldmaps and other config resources | info |

Every account, group, API key and role write through `/api/v1/auth` emits one event naming the actor (`admin_id`), the change and the target. Its `details` carry the roles, groups, members or permissions the write set, and the names of contact fields changed, never their values. No event carries a password, a hash, an attribute value or an API key's secret half.

Events flow through the OTel pipeline → JSON → ClickHouse → HyperDX.
No custom ClickHouse audit table — standard OTel log ingestion is used.

---

## 10. AuthContext Model

```python
class AuthContext(BaseModel):
    org_id: str = "default"  # Primary tenant
    user_id: str  # Required unique identifier
    email: str | None = None  # From the OIDC/JWT claim, for git author attribution
    roles: list[str]  # Resolved DFE roles
    grants: list[ScopedGrant] = []  # Roles with the scope each was bound at
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
    enabled: bool = True  # Off needs a dev DFE_ENV, else the engine refuses to start
    auth_dir: str = ""  # Path to config/auth/, OIDC providers in its oidc-providers/
    trust_proxy_auth_headers: bool = False  # Read the X-Oidc-* headers (Path 1)
    oidc_group_sync_enabled: bool = True  # Background sync of api-mode providers
    oidc_group_sync_tick_seconds: int = 60  # How often the sync checks which providers are due
    proxy_provider: str = "oidc"  # Provider name the header path stamps on accounts it provisions
    source_provider_bindings: dict[str, str] = {}  # e.g. {"scim": "entra"}
    api_key_default_ttl_days: int = 90  # 0 = keys created without expires_at never expire
```

Environment variables: `DFE_AUTH_ENABLED`, `DFE_AUTH_DIR`,
`DFE_AUTH_TRUST_PROXY_AUTH_HEADERS`, `DFE_AUTH_PROXY_PROVIDER`,
`DFE_AUTH_SOURCE_PROVIDER_BINDINGS` (a JSON object),
`DFE_AUTH_API_KEY_DEFAULT_TTL_DAYS`, `DFE_AUTH_OIDC_GROUP_SYNC_ENABLED`,
`DFE_AUTH_OIDC_GROUP_SYNC_TICK_SECONDS`. OIDC provider files always live in the
auth directory's `oidc-providers/`.

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
                OKTA_A["okta.py"]
            end
        end

        subgraph "resources/"
            ROLES_YAML["roles.yaml<br/>8 built-in roles"]
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

Not pursued. An app's shape is data in `dfe-infra/apps.yaml`, served through
`/api/v1/apps`, so there is no per-service surface registry and no
`/api/v1/service-surfaces` endpoint. The typed plugin system (`plugins.py`,
`plugins_builtin/`) still backs `/api/v1/services`.

---

## 14. Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Multi-role user | A platform grant with `query:execute` reads as the platform user; otherwise the caller must resolve to exactly one org, or gets `403 no_single_org` (section 5.2) |
| HyperDX unavailable | Non-fatal. Org CRUD succeeds. Sync retried in background |
| ClickHouse unavailable | `/readyz` reports NotReady while its ping fails. Liveness is untouched, so the pod is not restarted. The CH RBAC reconcile retries in the background (section 6.2) |
| Unknown OIDC group | No linked group with a role, so the sign-in is refused with `401 no_role_granted` and no account is created (section 2.1) |
| OIDC adapter failure | Failsafe: returns empty results, auth continues with available info |
| Default password in use | Dev `DFE_ENV`: warning at startup and `default_credentials: true` on setup-status. Any other `DFE_ENV`: the engine refuses to start (section 3.5) |

---

## 15. Non-Goals

- Per-metric RBAC granularity (access is per-service, not per-metric)
- Per-setting RBAC granularity (access is per-service config, not per-key)
- HyperDX per-user RBAC (solved at dfe-engine layer via teams)
- Dynamic K8s service discovery (future enhancement)
- Envoy Gateway SecurityPolicy CRD generation (managed by dfe-infra)
- ClickHouse cluster provisioning (managed by dfe-infra)
- Role hierarchy / inheritance (flat roles sufficient for 8-role set)

---

## 16. Breaking Changes from Pre-2.2

| What | Old | New |
|------|-----|-----|
| JWT library | `python-jose[cryptography]` | `PyJWT[crypto]` |
| Auth model | `AuthContext.permissions` field | Removed — resolved from roles at auth time |
| Role names | `admin`, `operator`, `viewer` | 8 granular roles in `roles.yaml` |
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
