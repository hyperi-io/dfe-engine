# OIDC Infrastructure Requirements — What dfe-engine Needs from dfe-infra

**Date:** 2026-04-01
**Source:** dfe-engine OIDC Provider Registry design (Phase 1.5)
**Status:** Requirements — dfe-engine is building the provider registry and
assumes dfe-infra handles everything below.

---

## Overview

dfe-engine manages OIDC provider configuration (which providers are attached,
group resolution, role mapping). It does NOT provision infrastructure, manage
Envoy Gateway SecurityPolicy CRDs, or create cloud IAM resources.

**dfe-infra owns:**
- Envoy Gateway deployment and SecurityPolicy CRDs
- Cloud IAM provisioning (Google service accounts, Entra app registrations, Okta apps)
- K8s Secret creation for OIDC credentials
- TLS certificates for OIDC callback URLs
- DNS for callback endpoints

**dfe-engine owns:**
- OIDC provider registry (YAML config files)
- Group resolution adapters (API calls to IdPs)
- Group-to-role mapping
- Auth header consumption from Envoy Gateway

---

## 1. Envoy Gateway OIDC SecurityPolicy

### What dfe-engine assumes exists

For each OIDC provider, dfe-infra must deploy an Envoy Gateway `SecurityPolicy`
CRD that handles the OIDC authorization code flow. dfe-engine reads the resulting
headers — it never configures Envoy directly.

### Required SecurityPolicy behaviour

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: SecurityPolicy
metadata:
  name: dfe-oidc-{provider-name}
  namespace: dfe-prod
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: dfe-engine-route
  oidc:
    provider:
      issuer: "{issuer_url}"             # From provider config
    clientID: "{client_id}"               # From K8s Secret
    clientSecret:
      name: dfe-oidc-{provider-name}     # K8s Secret name
      key: client-secret
    redirectURL: "https://{dfe_domain}/oauth2/callback"
    scopes:
      - openid
      - email
      - profile
    # CRITICAL: forward these headers to dfe-engine
    forwardAccessToken: false
    # Envoy extracts claims and forwards as headers:
    #   X-Oidc-Subject: user email or sub claim
    #   X-Oidc-Groups: comma-separated group IDs/names
```

### Headers dfe-engine expects

| Header | Content | Required |
|--------|---------|----------|
| `X-Oidc-Subject` | User email or unique ID from `sub` claim | Yes |
| `X-Oidc-Groups` | Comma-separated group IDs or names | Yes (can be empty) |
| `X-Forwarded-User` | Same as subject (nginx compat) | Optional |

**If `X-Oidc-Groups` is empty or missing**, dfe-engine still authenticates the
user (via subject) but resolves zero roles from groups. The user gets only
roles assigned directly via local account or manual group membership.

### Provider-specific scopes

| Provider | Extra Scopes Needed | Why |
|----------|-------------------|-----|
| Google | None in SecurityPolicy | Groups come from Admin SDK API, not the token |
| Entra ID | `GroupMember.Read.All` (Graph API, not OIDC scope) | Groups in token are GUIDs; names need Graph API |
| Okta | `groups` | Groups included in ID token via custom claim |
| Generic | Depends on provider | Whatever puts groups in the token |

---

## 2. K8s Secrets for OIDC Credentials

### What dfe-engine expects

Each OIDC provider needs credentials available as environment variables in the
dfe-engine pod. dfe-infra provisions these as K8s Secrets, mounted via pod spec.

### Per-provider secrets

**Google Workspace:**

| Secret Key | Env Var | Content |
|-----------|---------|---------|
| `client-id` | `DFE_OIDC_GOOGLE_CLIENT_ID` | Google OAuth2 client ID |
| `client-secret` | `DFE_OIDC_GOOGLE_CLIENT_SECRET` | Google OAuth2 client secret |
| `service-account-json` | `DFE_GOOGLE_SA_JSON` | Service account key JSON (for Admin SDK group resolution) |

K8s Secret name: `dfe-oidc-google` (or whatever the Helm chart configures)

**Entra ID:**

| Secret Key | Env Var | Content |
|-----------|---------|---------|
| `client-id` | `DFE_OIDC_ENTRA_CLIENT_ID` | Entra app registration client ID |
| `client-secret` | `DFE_OIDC_ENTRA_CLIENT_SECRET` | Entra app registration client secret |
| `tenant-id` | `DFE_OIDC_ENTRA_TENANT_ID` | Entra tenant ID (for Graph API calls) |

K8s Secret name: `dfe-oidc-entra`

**Okta:**

| Secret Key | Env Var | Content |
|-----------|---------|---------|
| `client-id` | `DFE_OIDC_OKTA_CLIENT_ID` | Okta app client ID |
| `client-secret` | `DFE_OIDC_OKTA_CLIENT_SECRET` | Okta app client secret |
| `api-token` | `DFE_OIDC_OKTA_API_TOKEN` | Okta API token (for Groups API, optional) |

K8s Secret name: `dfe-oidc-okta`

**Generic OIDC:**

| Secret Key | Env Var | Content |
|-----------|---------|---------|
| `client-id` | `DFE_OIDC_{NAME}_CLIENT_ID` | Provider client ID |
| `client-secret` | `DFE_OIDC_{NAME}_CLIENT_SECRET` | Provider client secret |

K8s Secret name: `dfe-oidc-{name}`

### Secret lifecycle

- Created by Terraform (via ESO from OpenBao/cloud SM) or manually by operator
- Mounted as env vars in dfe-engine pod spec (Helm chart values)
- dfe-engine reads at startup and when provider config changes
- Rotation: update the Secret, restart dfe-engine (or hot-reload if implemented)

---

## 3. Cloud IAM Provisioning (Terraform)

dfe-infra needs Terraform modules that provision the cloud-side resources
for each OIDC provider. dfe-engine does NOT do this — it just consumes
the credentials.

### 3.1 Google Workspace — `tf-oidc-google`

**Resources to create:**
1. Google Cloud project (or use existing)
2. OAuth2 consent screen (internal or external)
3. OAuth2 client ID (web application type)
   - Authorized redirect URI: `https://{dfe_domain}/oauth2/callback`
4. Service account for Admin SDK access
   - Grant `roles/admin.directory.group.readonly` (or domain-wide delegation)
5. Service account key (JSON) → store in secrets backend

**Outputs needed by dfe-engine:**
```hcl
output "client_id" { value = google_oauth2_client.dfe.client_id }
output "client_secret" { value = google_oauth2_client.dfe.client_secret, sensitive = true }
output "service_account_json" { value = google_service_account_key.dfe.private_key, sensitive = true }
output "issuer" { value = "https://accounts.google.com" }
```

**Google Admin Console manual step:**
If using domain-wide delegation, an admin must authorize the service account
in the Google Admin Console → Security → API Controls → Domain-wide delegation.
Scopes: `https://www.googleapis.com/auth/admin.directory.group.readonly`

### 3.2 Entra ID — `tf-oidc-entra`

**Resources to create:**
1. App registration in Entra ID
   - Redirect URI: `https://{dfe_domain}/oauth2/callback`
   - API permissions: `GroupMember.Read.All` (application type, admin consented)
2. Client secret (or certificate)
3. Optional: Enterprise application for user assignment

**Outputs needed by dfe-engine:**
```hcl
output "client_id" { value = azuread_application.dfe.client_id }
output "client_secret" { value = azuread_application_password.dfe.value, sensitive = true }
output "tenant_id" { value = data.azuread_client_config.current.tenant_id }
output "issuer" { value = "https://login.microsoftonline.com/${var.tenant_id}/v2.0" }
```

**Entra Admin Portal manual step:**
Admin must grant admin consent for `GroupMember.Read.All` in the app registration's
API permissions page. Without this, the Graph API call for group names fails.

### 3.3 Okta — `tf-oidc-okta`

**Resources to create:**
1. Okta application (OIDC Web type)
   - Redirect URI: `https://{dfe_domain}/oauth2/callback`
   - Groups claim: configure in authorization server → claims → add `groups` claim
2. API token (for optional Groups API access)

**Outputs needed by dfe-engine:**
```hcl
output "client_id" { value = okta_app_oauth.dfe.client_id }
output "client_secret" { value = okta_app_oauth.dfe.client_secret, sensitive = true }
output "issuer" { value = "https://${var.okta_domain}/oauth2/default" }
output "api_token" { value = okta_api_token.dfe.token, sensitive = true }
```

### 3.4 Generic OIDC — No Terraform module needed

Operator manually registers the OIDC client with their provider and provides
client_id + client_secret. dfe-infra just needs to create the K8s Secret.

---

## 4. Helm Chart Values

dfe-infra's Helm chart for dfe-engine needs to support mounting OIDC secrets.

### Required values.yaml additions

```yaml
# dfe-engine Helm chart values
oidc:
  enabled: false
  providers: []
  # Example:
  # providers:
  #   - name: google-workspace
  #     secretName: dfe-oidc-google
  #     envMappings:
  #       DFE_OIDC_GOOGLE_CLIENT_ID: client-id
  #       DFE_OIDC_GOOGLE_CLIENT_SECRET: client-secret
  #       DFE_GOOGLE_SA_JSON: service-account-json
  #   - name: entra-hyperi
  #     secretName: dfe-oidc-entra
  #     envMappings:
  #       DFE_OIDC_ENTRA_CLIENT_ID: client-id
  #       DFE_OIDC_ENTRA_CLIENT_SECRET: client-secret
  #       DFE_OIDC_ENTRA_TENANT_ID: tenant-id
```

The deployment template iterates over `oidc.providers` and creates env var
entries from each secret:

```yaml
# deployment.yaml (template snippet)
{{- range .Values.oidc.providers }}
{{- range $envVar, $secretKey := .envMappings }}
- name: {{ $envVar }}
  valueFrom:
    secretKeyRef:
      name: {{ $.secretName }}
      key: {{ $secretKey }}
{{- end }}
{{- end }}
```

---

## 5. Network Policies

dfe-engine needs outbound HTTPS access to IdP APIs for group resolution:

| Provider | Outbound Target | Port |
|----------|----------------|------|
| Google | `admin.googleapis.com` | 443 |
| Entra ID | `graph.microsoft.com`, `login.microsoftonline.com` | 443 |
| Okta | `{customer-domain}.okta.com` | 443 |
| Generic | Varies | 443 |

dfe-infra's network policies must allow dfe-engine pods to reach these
external APIs. If using a proxy, configure `HTTPS_PROXY` env var.

---

## 6. DNS and TLS

### Callback URL

All OIDC providers need a callback URL: `https://{dfe_domain}/oauth2/callback`

- dfe-infra manages DNS (wildcard or specific record)
- dfe-infra manages TLS (cert-manager + ACME)
- Envoy Gateway routes the callback to its OIDC handler
- dfe-engine never sees the callback — Envoy handles it entirely

### Per-deployment

| Deployment | Callback URL |
|-----------|-------------|
| DevEx | `https://dfe.devex.hyperi.io/oauth2/callback` |
| AWS | `https://dfe.{customer}.example.com/oauth2/callback` |
| On-prem | `https://dfe.{customer-domain}/oauth2/callback` |

---

## 7. What dfe-engine Does NOT Need from dfe-infra

- Token validation (dfe-engine trusts Envoy's headers, doesn't verify JWTs from IdPs)
- Session management (Envoy handles session cookies)
- OIDC discovery (Envoy handles `.well-known/openid-configuration`)
- Logout flow (future — out of scope for Phase 1.5)
- Multi-provider routing (one Envoy SecurityPolicy per route handles this)

---

## 8. Testing Infrastructure

For dfe-engine to test OIDC provider adapters against real IdPs, dfe-infra
needs to provide sandbox/test credentials:

| Provider | Test Setup |
|----------|-----------|
| Google | Test Google Workspace with a service account + domain-wide delegation |
| Entra ID | Test Entra tenant with app registration + admin-consented Graph API |
| Okta | Free Okta developer account with test groups |

These test credentials should be available as env vars in the CI environment
or as manually-configured local `.env` for development testing.

Integration tests in dfe-engine that need real IdP access will be marked
`@pytest.mark.skip(reason="Needs {provider} sandbox credentials")` and
run only when the credentials are available.

---

## Summary: Responsibilities

| Concern | dfe-infra | dfe-engine |
|---------|-----------|-----------|
| Envoy Gateway SecurityPolicy | Creates and manages CRDs | Reads headers only |
| Cloud IAM (Google SA, Entra app, Okta app) | Terraform modules provision | Reads credentials from env vars |
| K8s Secrets for OIDC creds | Creates via ESO/Terraform | Reads from env vars |
| Helm chart env var mounting | Deployment template | Declares what env vars it needs |
| Network policies (outbound to IdP APIs) | Allows egress | Makes HTTPS calls |
| DNS + TLS for callback URL | cert-manager + DNS | Not involved |
| OIDC provider config (which providers, settings) | Not involved | YAML files in config/auth/ |
| Group resolution (API calls to IdPs) | Not involved | Adapter modules |
| Group-to-role mapping | Not involved | GroupStore + RoleConfig |
| Auth header processing | Not involved | get_current_user() |
