# OIDC Provider Registry — Design Notes (Phase 1.5)

> **Status:** Requirements captured, not yet designed. Builds on Phase 1 RBAC foundation.

## Requirements

1. **CRUD for OIDC providers** — attach/detach providers via API (for Terraform, UI, CLI)
2. **Detach must clean up** — remove config artefacts, group mappings, cached tokens
3. **Provider-specific group resolution** — each IdP has a different way to get groups
4. **"Unfriendly mode"** — works with raw group IDs when cloud ops can't configure names
5. **Terraform modules** for each provider's extra config (Graph API permissions, Admin SDK delegation, etc.)

## Provider-Specific Group Resolution

| Provider | Groups in Token? | API for Names | Scope/Permission Needed |
|----------|-----------------|---------------|------------------------|
| **Okta** | Yes (custom claim mapping) | `GET /api/v1/users/{id}/groups` | `okta.groups.read` scope |
| **Entra ID** | GUIDs only (200+ = overage) | Graph API `GET /me/memberOf` | `GroupMember.Read.All` or `Directory.Read.All` |
| **Google** | No | Admin SDK Directory API | `admin.directory.group.readonly` (domain-wide delegation or admin SA) |
| **Generic OIDC** | Maybe | None | N/A — just use whatever comes in the token |

## Unfriendly Mode (Raw IDs)

When cloud ops can't or won't configure group claims or API access,
the system still works:

- OIDC token has groups as GUIDs (Entra) or nothing (Google)
- dfe-engine's group files can use GUIDs as names
- Admin manually maps GUIDs to roles via `dfe-api groups create {GUID} --roles data_analyst`
- No API call needed — just the OIDC token claims

This solves the "this will take months for cloud ops to do" problem.
The system is immediately usable with raw IDs, then upgrades to named
groups when API access is granted.

## Upgrade Path

```
Stage 1: "Unfriendly" — raw group IDs from OIDC token
    -> Admin manually creates group files with GUID names
    -> Works immediately, no extra API config needed

Stage 2: "Friendly" — API access granted
    -> dfe-engine periodically fetches group names from provider API
    -> Auto-creates/updates group files with human-readable names
    -> Existing GUID-based group files still work (backward compat)
```

## OIDC Provider Config (YAML)

```yaml
# config/auth/oidc-providers/entra-hypersec.yaml
name: entra-hypersec
type: entra_id
enabled: true

# OIDC discovery
issuer: "https://login.microsoftonline.com/{tenant_id}/v2.0"
client_id_env: DFE_OIDC_ENTRA_CLIENT_ID
client_secret_env: DFE_OIDC_ENTRA_CLIENT_SECRET

# Group resolution mode
groups:
  mode: api          # token_claim | api | manual
  # When mode=api, these are used:
  graph_api_scope: "GroupMember.Read.All"
  sync_interval: 3600  # seconds between group name syncs

# When mode=token_claim:
# groups:
#   mode: token_claim
#   claim_name: "groups"    # default OIDC claim

# When mode=manual:
# groups:
#   mode: manual
#   # Admin creates group files manually with GUIDs
```

## API Endpoints (Phase 1.5)

```
POST   /api/v1/auth/oidc-providers              # Attach new provider
GET    /api/v1/auth/oidc-providers              # List providers
GET    /api/v1/auth/oidc-providers/{name}       # Get provider config
PUT    /api/v1/auth/oidc-providers/{name}       # Update config
DELETE /api/v1/auth/oidc-providers/{name}       # Detach (clean up artefacts)
POST   /api/v1/auth/oidc-providers/{name}/sync  # Force group name sync
GET    /api/v1/auth/oidc-providers/{name}/test  # Test connectivity
```

## Terraform Modules (per provider)

```
terraform/modules/
    tf-oidc-entra/          # Entra ID app registration + Graph API permissions
    tf-oidc-google/         # Google Workspace OAuth + Admin SDK delegation
    tf-oidc-okta/           # Okta app + authorization server + groups claim
    tf-oidc-generic/        # Generic OIDC (just client_id/secret + issuer)
```

Each module outputs the config values needed for the dfe-engine OIDC provider YAML.

## Envoy Gateway Integration

The OIDC provider config in dfe-engine maps to Envoy Gateway SecurityPolicy:
- dfe-engine generates the SecurityPolicy CRD from the provider config
- Or the Terraform module creates the SecurityPolicy directly
- Either way, Envoy Gateway handles the OIDC flow; dfe-engine just reads headers

## Dependencies

- Phase 1 RBAC foundation (accounts, groups, roles — in progress)
- Envoy Gateway deployed (dfe-infra)
- Provider API credentials provisioned (Terraform or manual)
