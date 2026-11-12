# OIDC + RBAC architecture and design

The single source of truth for how dfe-engine handles authentication,
external OIDC, group->role mapping, the two RBAC planes, the management API
and the infrastructure it needs. The RBAC/multi-tenant ClickHouse detail lives in
[rbac.md](rbac.md), and this doc references it rather than repeating it.

Exhaustive reference (exact request/response shapes, tofu outputs, secret
keys) lives with its source of truth - `openapi.json`, the code
(`src/dfe_engine/auth/oidc/`), and the dfe-infra tofu modules - not
transcribed here where it would drift.

## Contents

1. The auth model - local always, OIDC additive
2. The layered pipeline
3. Layer 2 - the provider registry and adapters
4. The two RBAC planes
5. The management API and CLI
6. Infrastructure requirements
7. Provider-agnostic onboarding recipe

## 1. The auth model - local always, OIDC additive

The engine is the RP and the **single token issuer**. An external IdP does the
interactive login; the engine validates the IdP id_token (signature/nonce/aud/
exp against the IdP JWKS, via Authlib) and re-mints its own ES384 token.
Downstream (dfe-ui, dfe-hyperdx) only ever sees the engine token - the IdP's
token stays on the engine<->IdP leg. Code: `auth/oidc/rp.py`,
`api/v1/oidc_login.py`.

By design several auth paths run at once and merge; OIDC never replaces local
auth, it augments it:

- **Local accounts** (username/password -> JWT), **API keys**, and the
  **disabled/dev** path are always available - the fallback that keeps an
  operator in even if the IdP is down or misconfigured.
- **External OIDC** is added when provided (recommended for real deployments).
  When present it takes precedence, but the local and key paths stay live.
- **Groups from both sources unify.** An IdP group takes a DFE group's roles
  only through that group file's `source_provider` + `source_id` link. A
  matching name grants nothing (rbac-sign-in.md section 2.1). One role model,
  two merged identity sources.

The four auth paths, precedence, and the header-trust security precondition are
detailed in rbac.md section 1.

## 2. The layered pipeline

The whole design confines provider chaos to Layer 2 and reuses the existing
engine machinery above it:

```text
  [0] Trust / issuer      external IdP authenticates -> engine validates the
                          IdP id_token (JWKS) -> engine RE-MINTS its own ES384
                          token. Downstream sees ONLY the engine token.
  [1] Identity            NormalizedIdentity { subject(sub), email, raw_groups }
  [2] Group resolution    <-- ALL provider-specific mess lives HERE, behind an
      (per-provider            adapter. Raw token refs (names | ids | nothing)
       adapter)                -> NormalizedGroup { id, name, source }.
                               token_claim mode: refs in token.
                               api mode: refs absent/id-only -> call the
                                         provider directory API.
                          engine group file (config/auth/groups/*.yaml)
                          -> roles + org_ids + scope
                                    |
          +-------------------------+---------------------------+
   [3a] CONTROL-PLANE axis                       [3b] DATA-PLANE axis
        group -> static roles                         group -> org_ids
        (admin, data_analyst,                         (DYNAMIC: presence of
         infra_admin, ...)                             org_ids confers scoped
   [4a] dfe-engine authorization                       access - no named per-org
        roles -> permissions ->                        role)
        what you can DO (manage                   [4b] Observe data browsing
        sources/hunts/config/deploy)                   (Kibana-style)
                                                       org_ids -> dfe-hyperdx team +
                                                       the org's pinned CH user
                                                       -> row-filtered DATA VIEW
                                                       ONLY (read, no control)
```

Two principles:

1. **Provider-specific code stops at Layer 2.** Below it is generic OIDC
   plumbing; above it, generic machinery that does not care which IdP the user
   came from. Adding a provider = one adapter at Layer 2, nothing else changes.
2. **Control-plane and data-plane are orthogonal.** The same group membership
   feeds both, but control-plane roles decide what you can DO while org_ids
   decide what data you can SEE. A pure customer gets ONLY the data-plane axis.

## 3. Layer 2 - the provider registry and adapters

### Provider config

Providers are YAML files (one per provider, filename stem = name) managed by
`OIDCProviderRegistry`. The model (`auth/oidc/models.py`, the SSoT) carries:
`type` (generic|google|entra_id|okta), `enabled`, `issuer`, `client_id`,
`client_id_env`, `client_secret_path`, `client_secret_env`, `scopes`, and a
`groups` block (mode + claim_name + per-provider directory fields).

A provider file that does not load, whether its YAML is unreadable or its fields fail validation, is left out of the provider list, the relying party and the sync scheduler. The engine logs it once and counts it on `auth_oidc_providers_skipped_total{reason}` (`unreadable`, `invalid`), and every other provider carries on.

A secret is never stored in config: the client id is not secret and is held in
the clear, every other credential is a PATH into the `DfeSecrets` store or the
NAME of an env var. Resolution reads the store before the environment, so a
secret sent to the API works immediately while an ESO-mounted env var keeps
serving a provider configured that way.

### Group resolution modes

- `token_claim` (the default) - groups arrive as strings in the id_token; direct lookup.
- `api` - the token lacks group names (or carries only ids); an adapter calls
  the provider directory API to resolve names and enumerate membership on a
  schedule. Each provider syncs every `groups.sync_interval` seconds (60 at least) from a background loop in every engine replica; `DFE_ENGINE_AUTH_OIDC_GROUP_SYNC_ENABLED=false` leaves only `POST /oidc-providers/{name}/sync`.
- `manual` - membership mapped by hand in the engine; the token's groups are ignored at login and nothing syncs.

Which fields each provider type accepts and requires in each mode is enforced by the provider admin API, from the rules in `auth/oidc/field_rules.py`. A `generic` provider has no `api` mode and a `google` provider has only `api` mode, with `enrich_on_login` on.

### The normalized shape

Every provider's groups normalise to `NormalizedGroup { id, name, source }`.
`GroupInfo { id, name, email, description }` (models.py) already provides this
for api-mode; the token_claim/login path (`extract_identity`, rp.py) still emits
a flat `list[str]`, but the resolution gap it created is now CLOSED at the
matching layer: each group file is linked by `source_provider` and that provider's
`source_id`, and a login takes the groups whose `source_id` it asserts, for the
provider it signed in through (`membership.linked_groups`). A group's name links
nothing. So an Entra login carrying object GUIDs - which match no group filename -
resolves against `source_id` and lands the right roles. The sync, SCIM's `externalId` and an admin through the groups API or dfe-ui's group form each set the link, so a `token_claim` login carrying names (Okta, dex, Keycloak) lands its roles once a group's `source_id` is the name the IdP sends.
The adapter is the ONLY place the remaining quirks live:

| Quirk | Real cause | Adapter's job | Status |
|-------|-----------|---------------|--------|
| refs are names (dex/okta) vs ids (entra) vs absent (google) | provider design | normalise to {id,name}; resolve GUIDs by source_id, call the directory API when the token lacks the name | id->name resolution DONE (source_id match) |
| id stable, name mutable | directories rename groups | map roles on `id`, display `name` | source_id is the stable key |
| overage (entra >200 -> a Graph pointer) | token size limits | detect the marker, fall back to the directory API | DONE: `rp.py` detects `_claim_names` and fetches `transitiveMemberOf` with the user's own login token (`/me`: User.Read returns each group's id, which is all role resolution needs; `GroupMember.Read.All` adds names), falling back to app credentials (tenant id + client secret) when Graph refuses that token |
| nested / transitive groups | AD/entra/google nesting | take the closure the provider already emits, or walk it when it does not | MEASURED: entra emits the full closure in-token; honour it (per-provider fact, not a global toggle) |
| name collisions across tenants | bare names aren't unique | namespace the key (source+domain) | a name held by a group not linked to that IdP group is skipped (`name_taken`); namespacing not yet built |
| non-role groups (mailing lists) | not all groups grant access | filter by group type | not yet built |

### Adapters

`auth/oidc/adapters/`: `generic` (token_claim/manual, no API), `google` (Cloud
Identity Groups API with the user's own login token; the Admin SDK Directory API
as an optional service account), `entra` (Microsoft Graph), `okta` (Groups API). Contract:
`list_all_groups`, `resolve_user_groups`, `test_connection`. The two a login can
reach never raise: `resolve_user_groups` reports an outage or refusal. An outage
keeps the token's groups, else the account's. Anything else replaces them.
`list_all_groups` serves the sync only and raises when a page fails, so the sync
never reads a short listing as the whole directory.

### Sync and control-plane independence

`auth/oidc/sync.py` enumerates a provider's groups (api mode). It creates a group file for each new one, linked by `source_provider` and `source_id`, and updates only files already linked to that IdP group (their `source_id` is its id and their `source_provider` this provider, or one `auth.source_provider_bindings` joins to it), preserving their roles. A file of the same name with no link is skipped as `name_taken`: a display name is no identity, so linking one is an admin's act through the groups API or dfe-ui's group form (rbac-groups.md section 4.4). A new group whose name is a stored group's `source_id` or whose id is a stored group's name is skipped as `name_taken` too, since an API key resolves an identifier by name, then by `source_id`. A file carrying the id under any other provider, whatever its name, is skipped as `provider_mismatch`, so a sync never moves a group between providers or creates a second group carrying the id. Detaching a provider does NOT delete its groups - they orphan with `source_provider` set, reported on delete.

**Sign-in needs the engine.** It is the relying party and the only token
issuer, so with the engine down nobody can sign in, and provider CRUD and group
sync stop too. Running pipelines carry on: the data path does not call the
engine.

## 4. The two RBAC planes

One group resolution (`deps.py _resolve_group_grants` + the `Group` model) feeds
both. A group file carries `roles`, `org_ids`, and `scope`
(`system`|`org:<name>`); a user's roles and org_ids are the union across groups.

- **Control plane (3a/4a):** `roles.yaml` + wildcard `permission_matches` gate
  the dfe-engine API (manage sources/hunts/config/deploy). Static named roles.
- **Data plane (3b/4b):** org_ids drive dynamic, data-only access. A caller fenced to one org (an `org_viewer`, or any role bound at that org's scope) browses Observe as the org's pinned ClickHouse user, whose READONLY tenant setting and the shared RESTRICTIVE row policies filter every query to that org (rbac.md section 5). Read-only; no control-plane power.

Both axes already resolve end to end. The re-minted token carries
`{sub, email, groups}` plus the `role` claim dfe-hyperdx reads (see
auth-envoy-topology.md); roles AND org_ids still derive engine-side from the
group files, not the token. Full detail: rbac.md sections 2, 5, 6, 7.

## 5. The management API and CLI

Admin CRUD lives at `/api/v1/auth/oidc-providers` (`api/v1/oidc_providers.py`):
create / list / get / update / delete (delete reports orphaned groups) /
`{name}/sync` / `{name}/test` / `{name}/verify-login`. RBAC scopes
`oidc_read|write|delete`, audit on every mutation, pagination on list. The `dfe`
CLI gets these for free - generated from `openapi.json` - so UI and CLI share one
CRUD surface. Create/update take the credential VALUES (`client_secret`, the
Okta API token, the Entra client secret, the Google SA JSON), write them to the
secret store and record only the path, then re-register the provider with the
relying party - so a provider configured purely through the API completes a
login without an env edit or a restart. A `*_env` field takes an environment
variable NAME; a credential pasted there is a 422 naming the field. Detach
deletes the provider's stored secrets.

Verification for a thorough onboard/offboard workflow:

- **Login-config check - DONE.** `GET /{name}/verify-login` reports whether the
  client_id and client_secret resolve (value never returned) and whether the
  issuer's discovery document is reachable and well-formed. This is the login
  half that `{name}/test` (group-DIRECTORY creds only, a no-op for generic
  providers) never covered.
- **Consolidated introspection.** No single endpoint returns raw claims +
  normalized groups + resolved roles + org_ids for a login. Assemble from the
  callback JSON (`{subject,email,groups}`) + `GET /auth/me` (roles + org_ids,
  now that /auth/me returns org_ids)
  until a dedicated verify endpoint exists.

## 6. Infrastructure requirements

What a deployment must provide (dfe-infra owns these; the engine consumes them).

- **OIDC client credentials** per provider, as env vars from a K8s Secret
  (materialised by ESO from OpenBao / cloud SM). Env-var NAMES are declared in
  the provider config; the chart mounts the values. Per-provider secret keys
  (client-id, client-secret, and provider extras - Google SA JSON, Entra
  tenant-id, Okta api-token) are defined by the tofu modules, not here.
- **Cloud IAM provisioning** (tofu): `tf-oidc-google` (OAuth client is
  console-only - the module does the group-resolver SA), `tf-oidc-entra` (app
  registration + admin-consented `GroupMember.Read.All`), `tf-oidc-okta` (OIDC
  app + groups claim + API token). Modules live in dfe-infra; this engine just
  reads the resulting creds.
- **DNS + TLS** for the callback host. The native RP callback is
  `https://{dfe_host}/api/v1/auth/oidc/{provider}/callback` (+ localhost for dev).
- **Network egress** to IdP directory APIs for api-mode group resolution
  (`admin.googleapis.com`, `graph.microsoft.com`, `{org}.okta.com`).
- **Envoy Gateway SecurityPolicy - the FALLBACK path only.** The Envoy
  header-trust path (injecting `X-Oidc-Subject`/`X-Oidc-Groups`) is kept for
  deployments where the engine sits strictly behind Envoy. It is NOT the
  primary login path (the native RP is). When used, the engine MUST be reachable
  only via Envoy (NetworkPolicy), or headers are forgeable.

## 7. Provider-agnostic onboarding recipe

Because the mess is quarantined at Layer 2, onboarding any real external OIDC is
fixed:

1. Register a provider config (type + issuer + client env-var names + groups mode/claim).
2. The IdP emits whatever it emits, and the Layer-2 adapter normalises it.
3. Author group files mapping the normalised group id -> roles + org_ids.
4. Layers 3-4 do the rest unchanged.

Canonical role + group names are in [rbac-vocabulary.md](rbac-vocabulary.md) -
the SSoT every consumer (engine, ui, infra, deploy, schemas, test envs) aligns to.
