# OIDC Provider Registry — Design Specification

**Status:** Design (Phase 1.5)
**Depends on:** Phase 1 RBAC foundation (accounts, groups, roles, 4 auth paths)
**Infra requirements:** [docs/OIDC-INFRA-REQUIREMENTS.md](OIDC-INFRA-REQUIREMENTS.md)

---

## 1. Problem Statement

dfe-engine's OIDC header auth path (Phase 1) works — Envoy Gateway injects
`X-Oidc-Subject` + `X-Oidc-Groups`, dfe-engine resolves groups to roles.

What's missing:
1. **Provider lifecycle** — no way to attach/detach OIDC providers via API/CLI
2. **Group name resolution** — OIDC tokens often contain GUIDs (Entra) or nothing (Google), not human-readable names
3. **Unfriendly→friendly upgrade** — no path from "raw GUIDs" to "named groups"

## 2. Architecture

### Control Plane Independence (CRITICAL)

**dfe-engine is a management layer, NOT a runtime dependency.** The OIDC
auth flow works entirely without dfe-engine:

```
User → Envoy Gateway (OIDC SecurityPolicy — static K8s CRD)
     → dfe-engine reads X-Oidc-Subject + X-Oidc-Groups headers
     → GroupStore resolves groups to roles (from YAML files on disk)
```

If dfe-engine is down:
- Envoy Gateway keeps doing OIDC (CRD is static infrastructure)
- Existing group files on disk still resolve roles
- Users keep accessing the platform with their existing roles
- Only group sync and provider CRUD stop

**dfe-engine never generates Envoy Gateway SecurityPolicy CRDs.**
That's dfe-infra's job. See [OIDC-INFRA-REQUIREMENTS.md](OIDC-INFRA-REQUIREMENTS.md).

### Component Boundary

| Concern | Owner | Runtime dependency on dfe-engine? |
|---------|-------|----------------------------------|
| OIDC auth flow (redirects, tokens, cookies) | Envoy Gateway (dfe-infra) | **No** |
| SecurityPolicy CRD | dfe-infra Terraform/Helm | **No** |
| Cloud IAM (Google SA, Entra app, Okta app) | dfe-infra Terraform | **No** |
| K8s Secrets for OIDC credentials | dfe-infra ESO/Terraform | **No** |
| OIDC provider config (which providers, settings) | dfe-engine | N/A (it IS dfe-engine) |
| Group name resolution (API calls to IdPs) | dfe-engine | **No** (writes files; files persist) |
| Group-to-role mapping | dfe-engine GroupStore (YAML files) | **No** (files on disk) |

---

## 3. Data Model

### Provider Config

```
config/auth/oidc-providers/
    entra-corp.yaml
    google-workspace.yaml
    generic-sso.yaml
```

Each provider is a YAML file (DirectoryConfigStore pattern):

```yaml
# config/auth/oidc-providers/google-workspace.yaml
type: google                    # google | entra_id | okta | generic
enabled: true
display_name: "Google Workspace"

# OIDC discovery
issuer: "https://accounts.google.com"
client_id_env: DFE_OIDC_GOOGLE_CLIENT_ID

# Group resolution
groups:
  mode: api                     # token_claim | api | manual
  claim_name: ""                # For token_claim: which JWT claim has groups
  sync_interval: 3600           # For api: seconds between syncs (0 = manual only)

  # Google-specific (only read when type=google, mode=api)
  service_account_json_env: DFE_GOOGLE_SA_JSON
  admin_email: "admin@customer.com"
  domain: "customer.com"

# Metadata (managed by dfe-engine)
created_at: "2026-04-01T00:00:00Z"
last_sync_at: ""
last_sync_status: ""
sync_error: ""
```

### Provider Types and Group Modes

| Type | Group Modes Available | Notes |
|------|----------------------|-------|
| `generic` | `token_claim`, `manual` | No API — groups from token or manual mapping |
| `google` | `api`, `manual` | Admin SDK for group names. No groups in token. |
| `entra_id` | `token_claim`, `api`, `manual` | GUIDs in token; Graph API for names |
| `okta` | `token_claim`, `api`, `manual` | Groups in token natively; API optional |

### Group Mode Behaviour

| Mode | How groups arrive | dfe-engine action |
|------|------------------|-------------------|
| `token_claim` | In `X-Oidc-Groups` header (set by Envoy from token claim) | Direct lookup in GroupStore. No API calls. |
| `api` | GUIDs/IDs in header; names fetched from IdP API | Periodic sync writes group files with human-readable names. Auth uses GUIDs immediately (unfriendly), names after first sync (friendly). |
| `manual` | Admin creates group files via CLI/API | No automatic resolution. Admin maps GUIDs to roles manually. |

### Auto-Created Group Files

When sync runs, it creates/updates group files with a `source_provider` tag:

```yaml
# config/auth/groups/engineering@customer.com.yaml
# (auto-created by google-workspace provider sync)
description: "Engineering team"
roles: []                           # Admin assigns roles after sync
members: []
source_provider: "google-workspace" # Tracks which provider created this
source_id: "groups/03ep1234"        # Provider-native group ID
```

Groups with `source_provider` set are identified as auto-created for
detach cleanup warnings (option C — warn, don't delete).

---

## 4. Module Structure

```
src/dfe_engine/auth/oidc/
    __init__.py             # Package exports
    models.py               # OIDCProvider Pydantic model
    registry.py             # OIDCProviderRegistry (CRUD)
    sync.py                 # Background group sync runner
    adapters/
        __init__.py         # Adapter factory: get_adapter(provider) -> OIDCGroupAdapter
        base.py             # OIDCGroupAdapter ABC
        generic.py          # token_claim + manual (no API calls)
        google.py           # Google Admin SDK Directory API
        entra.py            # Microsoft Graph API
        okta.py             # Okta Groups API (stub initially)
```

### Adapter Interface

```python
class OIDCGroupAdapter(ABC):
    """Resolves OIDC group IDs to human-readable names."""

    @abstractmethod
    async def resolve_groups(self, group_ids: list[str]) -> dict[str, str]:
        """Map group IDs to display names.
        Unknown IDs returned as {id: id} (unfriendly fallback).
        Returns: {group_id: display_name}"""

    @abstractmethod
    async def list_all_groups(self) -> list[GroupInfo]:
        """Fetch all groups from the provider for initial/full sync.
        Returns list of GroupInfo(id, name, email, description)."""

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Test provider connectivity. Returns (success, message)."""
```

### Adapter Implementations

**GenericAdapter:**
- `resolve_groups`: returns `{id: id}` for all (no name resolution)
- `list_all_groups`: returns empty (no API)
- `test_connection`: always `(True, "Generic provider — no API to test")`

**GoogleAdapter:**
- Uses `google-auth` + `google-api-python-client` for Admin SDK
- Service account with domain-wide delegation or direct directory scope
- `list_all_groups`: `GET /admin/directory/v1/groups?domain={domain}`
- `resolve_groups`: `GET /admin/directory/v1/groups/{groupKey}`
- `test_connection`: attempts `list_all_groups` with `maxResults=1`

**EntraAdapter:**
- Uses `msal` for client credentials OAuth2 token
- `list_all_groups`: `GET /v1.0/groups?$select=id,displayName,mail`
- `resolve_groups`: `GET /v1.0/groups/{id}?$select=displayName,mail`
- `test_connection`: attempts `list_all_groups` with `$top=1`

**OktaAdapter (stub):**
- Uses API token via `Authorization: SSWS {token}` header
- `list_all_groups`: `GET /api/v1/groups`
- Stub initially — implementation deferred after Google + Entra

---

## 5. Sync Flow

### Periodic Sync

```
For each enabled provider where groups.mode == "api" and sync_interval > 0:
    1. Get adapter for provider.type
    2. adapter.list_all_groups() → list of GroupInfo
    3. For each group:
        a. Check if group file exists in GroupStore (by name or source_id)
        b. If exists: update description, keep existing roles/members
        c. If new: create group file with source_provider tag, empty roles
    4. Update provider.last_sync_at, last_sync_status
    5. On error: log warning, set provider.sync_error, continue
```

### Force Sync

`POST /api/v1/auth/oidc-providers/{name}/sync` triggers immediate sync
for one provider. Same flow as periodic, just on demand.

### Unfriendly → Friendly Upgrade

```
Stage 1 (Day 0): Provider attached with mode=manual
    → Admin creates group files: dfe groups create "abc123-guid" --roles data_analyst
    → Auth works immediately with raw GUIDs from X-Oidc-Groups

Stage 2 (Day N): Cloud ops grants API access, admin updates mode to "api"
    → dfe oidc-providers update entra-corp --mode api
    → Sync runs: fetches group names, creates named group files
    → GUID-based group files still work (not deleted)
    → Admin migrates: assigns roles to named groups, removes GUID groups
```

---

## 6. API Endpoints

```
POST   /api/v1/auth/oidc-providers              # Attach new provider (admin)
GET    /api/v1/auth/oidc-providers              # List providers (admin)
GET    /api/v1/auth/oidc-providers/{name}       # Get config (no secrets) (admin)
PUT    /api/v1/auth/oidc-providers/{name}       # Update config (admin)
DELETE /api/v1/auth/oidc-providers/{name}       # Detach + warn orphaned groups (admin)
POST   /api/v1/auth/oidc-providers/{name}/sync  # Force group sync (admin)
GET    /api/v1/auth/oidc-providers/{name}/test  # Test connectivity (admin)
```

### Detach Response (Option C)

```json
{
    "deleted": "google-workspace",
    "orphaned_groups": [
        {"name": "engineering@customer.com", "source_provider": "google-workspace", "roles": ["data_analyst"], "members": 3},
        {"name": "soc-team@customer.com", "source_provider": "google-workspace", "roles": [], "members": 0}
    ],
    "message": "Provider detached. 2 groups were auto-created by this provider and still exist. Delete them manually with dfe groups delete <name> if no longer needed."
}
```

---

## 7. CLI Commands

```bash
dfe oidc-providers create my-google --type google
  # Creates config file, prompts for required fields or reads from flags

dfe oidc-providers list
  # Table: name, type, enabled, mode, last_sync_at, status

dfe oidc-providers show my-google
  # Full config (secrets redacted)

dfe oidc-providers test my-google
  # Tests connectivity, prints result

dfe oidc-providers sync my-google
  # Force sync, prints groups found/created/updated

dfe oidc-providers update my-google --mode api --sync-interval 1800

dfe oidc-providers delete my-google
  # Warns about orphaned groups, confirms
```

---

## 8. Dependencies

### New pip dependencies

| Package | Version | Purpose | Used by |
|---------|---------|---------|---------|
| `google-auth` | >=2.38.0 | Google service account auth | Google adapter |
| `google-api-python-client` | >=2.170.0 | Admin SDK Directory API | Google adapter |
| `msal` | >=1.32.0 | Entra client credentials OAuth2 | Entra adapter |

Okta adapter uses `AsyncHttpClient` from pylib (no new dep).
Generic adapter has no external dependencies.

### Existing dependencies used

- `hyperi_pylib.http.AsyncHttpClient` — all HTTP calls to IdP APIs
- `hyperi_pylib.logger` — structured logging
- `dfe_engine.auth.groups.GroupStore` — group file CRUD
- `dfe_engine.yaml_utils` — YAML load/dump

---

## 9. Settings

```python
class OIDCSettings(BaseModel):
    providers_dir: str = Field(
        default="",
        description="OIDC provider config directory. Default: {auth_dir}/oidc-providers/",
    )
    sync_enabled: bool = Field(
        default=True,
        description="Enable background group sync for api-mode providers",
    )
    sync_on_startup: bool = Field(
        default=True,
        description="Run group sync for all providers on startup",
    )
```

Added to `AuthSettings`: `oidc: OIDCSettings = Field(default_factory=OIDCSettings)`

---

## 10. Testing Strategy

### Unit Tests (no external services)

| Area | Tests |
|------|-------|
| OIDCProvider model | Validation, defaults, type-specific fields |
| OIDCProviderRegistry | CRUD, list, YAML persistence |
| GenericAdapter | resolve_groups passthrough, test_connection always True |
| GoogleAdapter | Config parsing, credential loading (skip actual API calls) |
| EntraAdapter | Config parsing, token acquisition logic (skip actual API calls) |
| Sync flow | Group file creation/update with source_provider tag |
| Detach cleanup | Orphaned group detection and warning |
| API endpoints | Full CRUD via TestClient |
| CLI commands | Smoke test via Typer test runner |

### Control Plane Independence Tests (CRITICAL)

These tests verify the platform keeps working when dfe-engine is unavailable.

```python
class TestControlPlaneIndependence:
    """Verify auth works with ONLY static files — no running dfe-engine."""

    def test_oidc_auth_works_with_existing_group_files(self, tmp_path):
        """OIDC headers + group YAML files → correct roles.
        No provider registry, no sync, no API — just files."""
        # Create group files manually (as if sync ran earlier)
        groups_dir = tmp_path / "groups"
        groups_dir.mkdir()
        yaml_dump({"roles": ["data_analyst"], "members": []},
                  groups_dir / "engineering.yaml")

        group_store = GroupStore(groups_dir)
        roles, _ = _resolve_roles_from_groups(["engineering"], group_store)
        assert "data_analyst" in roles

    def test_auth_works_without_provider_registry(self, client):
        """Auth via OIDC headers works even if no providers are registered."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "user@example.com",
                "X-Oidc-Groups": "dfe-admins",
            },
        )
        assert resp.status_code == 200
        # Groups resolved from GroupStore, not from provider registry

    def test_group_files_persist_after_provider_detach(self, tmp_path):
        """Detaching a provider does NOT delete group files."""
        # Setup: provider + auto-created groups
        # Action: delete provider
        # Assert: group files still exist, roles still resolve

    def test_auth_unaffected_by_sync_failure(self, client):
        """If group sync fails (IdP unreachable), existing auth keeps working."""
        # Groups from previous sync still resolve roles

    def test_existing_roles_survive_engine_restart(self, tmp_path):
        """Group files on disk survive bootstrap — not overwritten."""
        # Pre-create group files with roles
        # Run bootstrap_auth (simulates engine restart)
        # Assert group files unchanged, roles intact
```

### Integration Tests (need real IdP — marked skip)

```python
@pytest.mark.skip(reason="Needs Google Workspace sandbox credentials")
class TestGoogleAdapterIntegration:
    async def test_list_all_groups(self): ...
    async def test_resolve_groups(self): ...
    async def test_connection(self): ...

@pytest.mark.skip(reason="Needs Entra ID sandbox credentials")
class TestEntraAdapterIntegration:
    async def test_list_all_groups(self): ...
    async def test_resolve_groups(self): ...
    async def test_connection(self): ...
```

These run when sandbox credentials are available (via env vars in CI or local `.env`).

---

## 11. Implementation Order

1. **OIDCProvider model + registry** (CRUD, YAML persistence)
2. **Adapter base + GenericAdapter** (token_claim + manual modes)
3. **API endpoints + CLI** (CRUD, test, sync)
4. **Control plane independence tests**
5. **EntraAdapter** (Graph API) — test against live Entra tenant (urgent, ~1 month left)
6. **GoogleAdapter** (Admin SDK) — test against Google Workspace
7. **Sync runner** (background periodic sync)
8. **OktaAdapter** (stub, implement when free tier available)

---

## 12. Non-Goals

- Envoy Gateway SecurityPolicy generation (dfe-infra's job)
- Cloud IAM provisioning (dfe-infra Terraform modules)
- Token validation/verification (Envoy handles this)
- Session management or logout flow (future)
- Multi-provider routing (one SecurityPolicy per HTTPRoute, dfe-infra manages)
- SCIM provisioning (future — if needed for user lifecycle)
