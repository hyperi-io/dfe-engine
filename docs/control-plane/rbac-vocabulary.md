# RBAC vocabulary - roles, groups and terms

One vocabulary, used verbatim by every moving part: dfe-engine, dfe-ui,
dfe-infra, dfe-deploy, dfe-schemas, and the devex test environments. If a role
or group name appears in two repos it means the same thing in both, or it is a
bug.

This exists because the alternative is what we had: ArgoCD knowing about
`dfe-admins` but not `dfe-infra`, a second control plane calling the analyst role
`ANALYST_USER`, and a test fixture inventing `dfe-infra-admins` alongside the
`dfe-infra` the engine already seeds. Every one of those is a silent
authorisation gap - a user who looks authorised in one system and is invisible to
the next.

> **dfe-engine owns this vocabulary.** Roles are defined in
> [`src/dfe_engine/auth/resources/roles.yaml`](../../src/dfe_engine/auth/resources/roles.yaml)
> and default groups in
> [`src/dfe_engine/auth/bootstrap.py`](../../src/dfe_engine/auth/bootstrap.py).
> This document is the human-readable face of those two files. Adding or
> renaming anything starts here and in them, never in a consumer.

Related: [oidc-rbac-architecture.md](oidc-rbac-architecture.md) for how external
identities reach these roles; [rbac.md](rbac.md) for enforcement and the
ClickHouse tenant model.

## The two planes

Roles split across two orthogonal planes, and conflating them is the most common
modelling mistake:

- **Control plane** - what you can DO. Static roles granting API actions:
  configure sources, run hunts, deploy services, manage groups.
- **Data plane** - what you can SEE. Dynamic, driven by `org_ids`, and
  restricted to data browsing (HyperDX, Kibana-style). A user can be a powerful
  analyst on the control plane and see exactly one org's rows.

A role is not "more senior" than another across planes. `customer_viewer` is not
a junior `admin`; it is a different axis.

## Canonical roles

Defined in `roles.yaml`. `scoped: true` means the role binds within an org
rather than system-wide.

| Role | Plane | For |
|---|---|---|
| `admin` | control | Full access to all resources and all orgs |
| `infra_admin` | control | Service configs, deployments, Helm, ArgoCD, orgs, groups, Governed Ops |
| `infra_viewer` | control | Infrastructure read-only |
| `data_analyst` | control | Hunt, query, source, fieldmap, alert, rule, transform CRUD |
| `data_analyst_viewer` | control | Analyst scope, read-only |
| `data_viewer` | data | HyperDX dashboards and query execution |
| `dfe_operator` | control | Curated operational dials - invoke shipped actions, read governed state |
| `customer_viewer` | data | Org-restricted data viewer (`scoped: true`) |

## Canonical default groups

The engine SEEDS the first four on bootstrap when the groups directory is empty.
They are not suggestions - a fresh install has them, so every consumer can rely
on them existing.

| Group | Roles | Seeded | For |
|---|---|---|---|
| `dfe-admins` | `admin` | yes | Full access |
| `dfe-analysts` | `data_analyst` | yes | Analyst control plane |
| `dfe-viewers` | `data_viewer` | yes | Dashboards and query |
| `dfe-infra` | `infra_admin` | yes | Service and deployment management |
| `dfe-infra-viewers` | `infra_viewer` | no | Infrastructure read-only |
| `dfe-analyst-viewers` | `data_analyst_viewer` | no | Analyst read-only |
| `dfe-operators` | `dfe_operator` | no | Curated ops dials |
| `dfe-<org>-viewers` | `customer_viewer` | no | Per-org data plane, `scope: org:<org>` |

Note `dfe-infra`, not `dfe-infra-admins`. The engine seeded that name first and
it stands; a parallel name would mean two groups granting one role.

## Naming conventions

- **Roles**: `snake_case`, singular, describing the person - `data_analyst`, not
  `DataAnalystRole` or `ANALYST_USER`.
- **Groups**: `kebab-case`, plural, `dfe-` prefixed - `dfe-analysts`. The prefix
  matters because these names land in customers' own IdPs alongside their groups.
- **Per-org groups**: `dfe-<org>-viewers`, paired with `scope: org:<org>`.
- **Permissions**: `resource:action`, colon-separated, `*` wildcards -
  `hunt:read`, `service:*:config:*`.
- **Orgs / tenants**: the term is **org** everywhere in the product surface
  (`org_id`, `org_ids`, `scope: org:<name>`). "Tenant" appears only where
  ClickHouse's own vocabulary demands it (`current_tenant_id` row policies).
  One concept, and the boundary between the two words is exactly that setting.

## Who consumes what

| Repo | Consumes | How |
|---|---|---|
| dfe-engine | roles + groups | Defines them; enforces via `_resolve_group_grants` and `RoleConfig` |
| dfe-ui | roles | Shows/hides UI by resolved role from `GET /auth/me`; must not hardcode its own names |
| dfe-infra | groups | ArgoCD RBAC group mappings; ESO secret wiring for providers |
| dfe-deploy | groups | Deployment-time group seeding |
| dfe-schemas | orgs | `org_id` column semantics for tenant row policies |
| devex test env | both | Fixture renders these names into dex/entra/okta - see hyperi-infra `subprojects/dfe-oidc-testing/fixture.yaml` |

## Rules

1. **Add roles here first.** A role that exists in a consumer but not in
   `roles.yaml` grants nothing - unknown groups resolve to zero roles, which
   presents as a broken IdP rather than a config error.
2. **Never rename in a consumer.** Renaming propagates from this document and
   the two engine files outward, in one change.
3. **Group names are external contract.** They are created in customers' Entra,
   Okta and Workspace directories. Renaming one is a customer-facing migration,
   not a refactor.
4. **Map roles on stable ids where the provider has them**, and display names.
   Entra emits group GUIDs, not names; see the OIDC architecture doc.

## Known divergence

`dfe-control-plane` uses a different role vocabulary - `ADMIN`, `ANALYST_USER`,
`DATA_OWNER`, `HUNT_OWNER`, `INGEST_OWNER`, `INFRASTRUCTURE_OWNER`, plus
ClickHouse-side roles like `analyst_tier2`. Its group names (`dfe-admins`,
`dfe-analysts`) do agree with this document; only the roles diverge.

It is not deployed by dfe-infra, so nothing in the current deployment path
depends on that vocabulary. Reconciling it is a product decision - which of the
two role models survives - and is deliberately NOT resolved here.
