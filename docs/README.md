# dfe-engine docs index

Engineering docs for the dfe-engine config control plane. Grouped by theme below.
Layout is currently FLAT (files live directly under `docs/`); the theme headings
are the intended grouping if a subdir move happens later.

**Status legend:**

| Status | Meaning |
|--------|---------|
| Current | Reflects shipped code; authoritative. |
| Standard | A rule/standard to follow (not just description). |
| Design | Design spec; may lead or track implementation. |
| Reference | Integration / how-to reference. |
| Research | Exploratory background; not authoritative. |

The RBAC/tenant-isolation/HyperDX, OIDC, and Sigma docs were rewritten for the
engine-upgrade redesign (role rename + config-driven HyperDX block, fixed-user
custom-settings tenant isolation, GA one-team HyperDX, org-domain isolation chain,
full Sigma pipeline).

---

## Architecture and platform

| Doc | Status | What |
|-----|--------|------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Current | System architecture, module map, data flow. |
| [dfe-infra.md](dfe-infra.md) | Reference | The engine <-> infra boundary + deployment contract. |
| [BACKING-SERVICES.md](BACKING-SERVICES.md) | Current | Backing-service swap matrix (CH / Kafka / secrets / storage). |
| [SUPPLY-CHAIN-PINNING.md](SUPPLY-CHAIN-PINNING.md) | Standard | SHA-pin + cooldown + LTS dependency policy. |
| [HELM-COMPILER-RESEARCH.md](HELM-COMPILER-RESEARCH.md) | Research | Helm values compiler background. |
| [MONOREPO-MIGRATION.md](MONOREPO-MIGRATION.md) | Research | Monorepo question / migration notes. |
| [stack-research.md](stack-research.md) | Research | Stack selection background. |

## Data and query

| Doc | Status | What |
|-----|--------|------|
| [SCHEMA.md](SCHEMA.md) | Current | Meta schema, DDL generation, JSON promotion. |
| [SOURCE.md](SOURCE.md) | Current | Data sources, registries, source config. |
| [QUERY-API.md](QUERY-API.md) | Current | Parameterized query registry + execution API. |
| [PARAM-QUERIES.md](PARAM-QUERIES.md) | Reference | Parameterized-query authoring. |
| [SYNC.md](SYNC.md) | Current | Config sync + gitops survivability. |

## Detection

| Doc | Status | What |
|-----|--------|------|
| [SIGMA.md](SIGMA.md) | Current | Sigma pipeline: providers -> catalogue -> views -> rules -> hunts. |
| [HUNT-RUNNER-SCALING.md](HUNT-RUNNER-SCALING.md) | Design | Pull-based multi-pod hunt runner + KEDA scaling. |
| [HUNT-SCHEDULE-SMOOTHING.md](HUNT-SCHEDULE-SMOOTHING.md) | Design | Hunt schedule spread (incl the maths derivation). |
| [EXPRESSIONS-CEL.md](EXPRESSIONS-CEL.md) | Standard | The CEL expression standard + SQL transpiler. |

## Auth and tenancy

| Doc | Status | What |
|-----|--------|------|
| [RBAC.md](RBAC.md) | Current | Roles, config-driven HyperDX block, tenant isolation, deploy-repo authority. |
| [OIDC.md](OIDC.md) | Current | OIDC providers, group mapping, worked examples, infra contract. |

## API, UI and CLI

| Doc | Status | What |
|-----|--------|------|
| [UI-API-GUIDE.md](UI-API-GUIDE.md) | Reference | API guide for the dfe-ui consumer. |
| [DFE-UI-GUIDE.md](DFE-UI-GUIDE.md) | Reference | dfe-ui integration guide. |
| [AUTOMAGIC-TYPE-SAFETY.md](AUTOMAGIC-TYPE-SAFETY.md) | Reference | Contract-first type-safety approach. |
| [DFE-CLI-DESIGN.md](DFE-CLI-DESIGN.md) | Design | dfe-api CLI design. |

## Process and operations

| Doc | Status | What |
|-----|--------|------|
| [ANTI-PATTERNS.md](ANTI-PATTERNS.md) | Reference | Reviewed anti-pattern audit (keep/change verdicts + blocking-I/O work items). |
| [GITOPS-COMMIT-STANDARD.md](GITOPS-COMMIT-STANDARD.md) | Standard | Gitops commit format for governed writes. |
| [GOVERNED-OPS-DESIGN.md](GOVERNED-OPS-DESIGN.md) | Design | Generic YAML-in-git CRUD engine. |
| [OBSERVABILITY-STANDARD.md](OBSERVABILITY-STANDARD.md) | Standard | Whole-stack logs/metrics/traces to one OTel destination. |
| [HYPERDX-FORK-MAINTENANCE.md](HYPERDX-FORK-MAINTENANCE.md) | Reference | Maintaining the HyperDX fork (see RBAC.md section 8.5 for the tenant-setting deps). |

## Spikes

- `oauth2/` - OIDC / oauth2-proxy / Envoy spike configs (see [OIDC.md](OIDC.md) for the current model).
- `superpowers/` - plans and specs (out of scope for this index).
