# RBAC vocabulary - roles, groups and terms

One vocabulary, used verbatim by every moving part: dfe-engine, dfe-ui,
dfe-infra, dfe-deploy, dfe-schemas, and the test environments. If a role
or group name appears in two repos it means the same thing in both, or it is a
bug.

> **dfe-engine owns this vocabulary.** Roles are defined in
> [`src/dfe_engine/auth/resources/roles.yaml`](../../src/dfe_engine/auth/resources/roles.yaml)
> and default groups in
> [`src/dfe_engine/auth/bootstrap.py`](../../src/dfe_engine/auth/bootstrap.py).
> This document is the human-readable face of those two files. Adding or
> renaming anything starts here and in them, never in a consumer.

Related: [oidc-rbac-architecture.md](oidc-rbac-architecture.md) for how external
identities reach these roles, and [rbac.md](rbac.md) for enforcement and the
ClickHouse tenant model.

Roles split across two planes, and conflating them is the most common modelling
mistake. The control plane is what you can DO: static roles granting API actions,
such as configuring sources, running hunts, deploying services and managing
groups. The data plane is what you can SEE: driven by `org_ids` and limited to
data browsing in Observe, so a user can be a powerful analyst on the control
plane and see one org's rows. `org_viewer` is not a junior `admin`. It is a
different axis.

## Roles and default groups

Roles are defined in `roles.yaml`. `scoped: true` means the role never reads across orgs: its holders are held to their orgs wherever it is bound, system-wide included.

| Role | Plane | For |
|---|---|---|
| `admin` | control | Full access to all resources and all orgs |
| `infra_admin` | control | Service configs, deployments, Helm, Argo CD, orgs, groups, Governed Ops |
| `infra_viewer` | control | Infrastructure read-only |
| `data_analyst` | control | Hunt, query, source, fieldmap, alert, rule, transform CRUD |
| `data_analyst_viewer` | control | Analyst scope, read-only |
| `data_viewer` | data | Search dashboards and query execution |
| `dfe_operator` | control | Curated operational dials - invoke shipped actions, read governed state |
| `ingest_admin` | control | Machine feeds: mint and rotate an org's ingest credentials (`org:ingest_credentials:write`), create and edit sources. No org create or delete |
| `org_viewer` | data | Org-restricted data viewer (`scoped: true`) |

`org_viewer` is the one role hard-tied to the org CRUD: a user or group holding it is associated with one or more `org_ids`, and that association IS the grant. When such a user browses data through the Observe iframe inside dfe-ui, dfe-hyperdx connects as that org's pinned ClickHouse user. That user's READONLY tenant setting and the shared RESTRICTIVE row policies filter every query to the org's `_org_id` values. There is no unfiltered path: the scoping lives in the ClickHouse user, not in anything the UI or the user sends ([rbac.md](rbac.md) sections 5 and 6).

The engine SEEDS the first five default groups on bootstrap when the group store is empty.
They are not suggestions: a fresh install has them, so every consumer can rely
on them existing.

| Group | Roles | Seeded | For |
|---|---|---|---|
| `dfe-admins` | `admin` | yes | Full access |
| `dfe-analysts` | `data_analyst` | yes | Analyst control plane |
| `dfe-viewers` | `data_viewer` | yes | Dashboards and query |
| `dfe-infra` | `infra_admin` | yes | Service and deployment management |
| `dfe-ingest` | `ingest_admin` | yes | Machine feeds, for an API key that mints ingest credentials and edits sources |
| `dfe-infra-viewers` | `infra_viewer` | no | Infrastructure read-only |
| `dfe-analyst-viewers` | `data_analyst_viewer` | no | Analyst read-only |
| `dfe-operators` | `dfe_operator` | no | Curated ops dials |
| `dfe-<org>-viewers` | `org_viewer` | no | Per-org data plane, `scope: org:<org>` |

The admin group is `dfe-infra`, not `dfe-infra-admins`. The engine seeded that
name first and it stands, because a parallel name would mean two groups granting
one role.

## Naming, rules and consumers

- Roles: `snake_case`, singular, describing the person - `data_analyst`, not
  `DataAnalystRole` or `ANALYST_USER`.
- Groups: `kebab-case`, plural, `dfe-` prefixed - `dfe-analysts`. The prefix
  matters because these names land in customers' IdPs beside their groups.
- Per-org groups: `dfe-<org>-viewers`, paired with `scope: org:<org>`.
- Permissions: `resource:action`, colon-separated, `*` wildcards -
  `hunt:read`, `service:*:config:*`.
- Orgs and tenants: the term is **org** everywhere in the product surface (`org_id`, `org_ids`, `scope: org:<name>`). "Tenant" appears inside the ClickHouse isolation model alone (the tenant role, the tenant setting and its row policies), and that model is the boundary between the two words.

Four rules follow:

1. Add roles here first. A role a consumer uses that is not in `roles.yaml`
   grants nothing: unknown groups resolve to zero roles, which presents as a
   broken IdP rather than a config error.
2. Never rename in a consumer. Renaming propagates from this document and
   the two engine files outward, in one change.
3. Group names are external contract. They are created in customers' Entra,
   Okta and Workspace directories, so renaming one is a customer-facing
   migration, not a refactor.
4. Map roles on stable ids where the provider has them, and display names.
   Entra emits group GUIDs, not names. See the OIDC architecture doc.

Each repo consumes the vocabulary this way:

| Repo | Consumes | How |
|---|---|---|
| dfe-engine | roles + groups | Defines them, enforces via `_resolve_group_grants` and `RoleConfig` |
| dfe-ui | roles | Shows/hides UI by resolved role from `GET /auth/me`, and must not hardcode names of its making |
| dfe-infra | groups | `adminGroups` in `argocd/values/common.yaml` (`dfe-admins`, `dfe-infra`) and the Argo CD `policy.csv` that `bootstrap/argocd_login.py` writes |
| dfe-deploy | nothing | Ships no group files. The engine seeds the default groups at boot |
| dfe-schemas | orgs | `org_id` column semantics for tenant row policies |
| Test environments | groups | dfe-infra's `dfe-ops idp` fixture renders these group names for its dex test IdP (`bootstrap/fixtures/tester-idp-groups.toml`) |
