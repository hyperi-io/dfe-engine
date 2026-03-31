## CI: Migrated to hyperi-ci

CI now uses `hyperi-io/hyperi-ci` reusable workflows (PyPI: `hyperi-ci`).
No more `ci/` submodule. The `[skip ci]` requirement is lifted.

### Known Technical Debt: Mock Usage

Test suite uses `unittest.mock` (MagicMock, patch) in several test files.
This violates the no-mocks policy. Tests that mock ClickHouse clients and
Apprise notifications should be migrated to real dependencies (testcontainers
for ClickHouse, sandbox endpoints for notifications).

**Affected files:**
- `tests/unit/test_hunts/test_alert_grouping.py` — MagicMock for ClickHouse, patch for apprise
- `tests/unit/test_hunts/test_explain_hunts.py` — MagicMock for ClickHouse
- Other test files using `pytest-mock` fixtures

**Policy:** Do not add new mocks. Migrate existing mocks to real deps when touching those files.

---

# Project Context

**Project:** DFE Engine
**Purpose:** Core library and API server for the Data Fusion Engine — data stream management, hunt execution, ClickHouse integration, and Kubernetes deployment orchestration.

---

## DO NOT ADD TO THIS FILE

**The following belong elsewhere:**

| Data | Correct Location |
|------|------------------|
| Version numbers | `VERSION` file, `git describe --tags` |
| Tasks/Progress | `TODO.md` |
| Session history | Git log (`git log --oneline -10`) |
| Changelog | `CHANGELOG.md` (semantic-release) |
| Dates | Git commit timestamps |

**This file is for static project context only.**

---

## Product Principle: Generic, Not HyperI-Specific

**dfe-engine is a product, not an internal tool.** All code must be generic and
deployment-agnostic. HyperI is ONE deployment of DFE — never hardcode HyperI
domains, tenant IDs, infrastructure endpoints, or provider-specific assumptions.

- **dfe-engine** = generic product code. Config cascade handles deployment specifics.
- **`/projects/hyperi-infra`** = HyperI's specific DevEx infrastructure. NOT dfe-infra.
- **`/projects/dfe-infra`** = generic DFE infrastructure IaC. Test WITH it, never code FOR it.
- OIDC providers, ClickHouse, HyperDX, Envoy — all config-driven, zero hardcoded values.

## Product Principle: Control Plane, Not Critical Path

**dfe-engine is a management layer, NOT a runtime dependency.** If dfe-engine
is down (restart, crash, upgrade), the entire DFE platform MUST continue
running normally. Users just can't change anything until it comes back.

**When dfe-engine is down, these MUST keep working:**
- Envoy Gateway OIDC (SecurityPolicy is a static K8s CRD)
- ArgoCD sync (RBAC CSV baked into ConfigMap at deploy time)
- Rust services (config from YAML files / ConfigMaps)
- ClickHouse (users, row policies, data all persistent)
- HyperDX (connections persistent in MongoDB)
- KEDA autoscaling (ScaledObjects are static CRDs)

**When dfe-engine is down, these stop working:**
- Config changes (service configs, deployment configs, sources)
- Account/group/API key CRUD
- OIDC group sync (existing groups still work, new groups don't sync)
- Org CRUD (existing orgs keep working)
- HyperDX connection sync (existing connections keep working)
- Helm values compilation
- REST API and CLI

**Design implication:** dfe-engine must NEVER be in the runtime request path
of any other component. It writes config/state that other components read
independently. No component should call dfe-engine's API at request time.

---

## Project Overview

### Architecture

- **YAML SSoT** — all config storage uses YAML directories via `DirectoryConfigStore` (hyperi-pylib). No PostgreSQL in dfe-engine.
- **dfe-engine = library + API server** — `dfe-control-plane` is deprecated; its API is absorbed into dfe-engine
- **Rust services** (receiver, loader, archiver) read YAML config files directly — no Python at runtime
- **Contract-first API** — `openapi-spec/openapi.json` is committed; Prism mock via `docker-compose.dev.yaml`

### Key Components

1. **`api/`** — FastAPI REST API, JWT auth, RBAC, pagination, OpenAPI spec generation
2. **`hunts/`** — HuntEngine, Rule model, RuleCreationService, HdxSanitizer, alert grouping + cooldown
3. **`source/`** — Source model (top-level data stream abstraction), TypeRegistry, ExpressionValidator
4. **`services/`** — ServiceConfigRegistry, schema-less CRUD for Rust service runtime configs
5. **`deployment/`** — DeploymentConfigRegistry, K8s/KEDA config, t-shirt sizing, schema-less mode
6. **`helm/`** — HelmValuesCompiler: merges service + deployment + environment → Argo CD values
7. **`schema/`** — v2 YAML→DDL pipeline, DDLFileWriter, ViewGenerator
8. **`fieldmap/`** — FieldMap model, registry, resolver, ViewGenerator
9. **`auth/`** — Engine RBAC, open-ended `argo:<resource>:<action>` namespace, LocalAuthProvider
10. **`ai/`** — AIModuleInterface ABC, typed modules, AIModuleRegistry

### Tech Stack

- **Language:** Python 3.12
- **Framework:** FastAPI + Pydantic v2
- **Database:** ClickHouse (via clickhouse-connect)
- **Config:** YAML via DirectoryConfigStore (hyperi-pylib)
- **Deployment:** Kubernetes via Helm + Argo CD
- **Testing:** pytest + pytest-xdist (parallel)

---

## Key Decisions

### Config Storage: YAML SSoT (no PostgreSQL)

**Decision:** DirectoryConfigStore (hyperi-pylib) for all config — service configs, queries, sources, alert destinations.
**Rationale:** Single source of truth, git-aware writes, in-memory cache, no database dependency at runtime.

### Schema-less Service + Deployment Registries

**Decision:** Both `ServiceConfigRegistry` and `DeploymentConfigRegistry` support unknown services via raw dict fallback.
**Rationale:** Adding a new Rust service or new config key must not require a Python change in dfe-engine. The UI manages helm vars owned by Rust services.
**How:** `get_config()` tries typed plugin first, falls back to raw dict. `_parse_table_name()` falls back to last-hyphen split for unknowns.

### Helm Values Architecture

**Decision:** Two separate registries compose the full Helm values:
- `config:` section → service runtime config (what Rust reads directly) → `ServiceConfigRegistry`, stored in `services/` dir
- `keda:`, `resources:`, `replicas:` → K8s deployment metadata → `DeploymentConfigRegistry`, stored in `deployment/` dir
- `HelmValuesCompiler` merges both + injects source routing → final Argo CD values

**Reference charts:**
- `/projects/dfe-loader/chart/values.yaml` — `config.kafka`, `config.clickhouse`, `config.routing` + KEDA + resources
- `/projects/dfe-receiver/chart/values.yaml` — `config.kafka`, `config.server`, `config.otlp`, `config.routing` + KEDA + resources

### Simplified Type System (13 Primitives)

**Decision:** `string`, `text`, `integer`, `float`, `boolean`, `datetime`, `timestamp`, `date`, `ip`, `uuid`, `json`, `geo_point`, `enum`. Behaviour via `use_case` and `attribute` annotations, resolved by `TypeRegistry`.

### API: Contract-First Development

**Decision:** `openapi-spec/openapi.json` is the single source of truth. Enables Prism mock, openapi-typescript, MSW, and CI drift detection.

### Observability: OTEL replaces Prometheus

**Decision:** Metrics push to OTEL Collector → ClickHouse → HyperDX (ClickStack). Implemented in Helm compiler.

### Argo CD RBAC: Open-Ended Action Namespace

**Decision:** `argo:<resource>:<action>` prefix — NOT a hardcoded lookup table.

### Alert Grouping: No Suppression

**Decision:** ALL matched rows written to results table at full fidelity. Grouping is read-time (post-INSERT GROUP BY). Cooldown per `(hunt, rule, customer, group_key)` via `dfe_audit.alert_state` (ReplacingMergeTree), fail-open on errors.

### CEL Expression Language

**Decision:** Unified expression standard across Python + Rust.
- Python: `common-expression-language` v0.5.6 (PyO3 bindings to `cel-interpreter` Rust crate)
- Rust: `cel-interpreter` v0.10.0 (feature-gated `expression` in hyperi-rustlib)
- DFE profile restricts CEL to high-perf subset: no map/filter/exists/all/timestamp/duration
- `has()` macro requires member access: `has(obj.field)` not `has(x)`
- Transpiler (`transpiler.py`): tokenizer → Pratt parser → AST → SQL emitter

### Ingress: Envoy Gateway

**Decision:** nginx-ingress community EOL March 2026. Envoy Gateway has native OIDC via SecurityPolicy.

### Auth: Four Authentication Paths (Design — not yet implemented)

**Decision:** Four auth paths, checked in order:
1. **OIDC headers** — production (Envoy Gateway fronted). Trusts `X-Oidc-Subject` + `X-Oidc-Groups`.
2. **API key** (`X-API-Key` header) — machine-to-machine (CI/CD, Terraform, automation). Long-lived, bcrypt-hashed in config.
3. **JWT Bearer** — standalone/Docker users. Issued by `/api/v1/auth/login` (LocalAuthProvider).
4. **Disabled** — dev/test default, root AuthContext.

All credential storage in env vars / K8s Secrets. Role assignments in YAML (`assignments.yaml`). Account definitions in `local_accounts.yaml`. Zero credentials in YAML files.

### RBAC: YAML-Defined Granular Roles (Design — not yet implemented)

**Decision:** Roles defined in `config/rbac/roles.yaml` as collections of permission strings. Identity→role mapping in `assignments.yaml` (OIDC groups or local users). Default roles: admin, data_analyst, data_analyst_viewer, data_viewer, infra_admin, infra_viewer, customer_viewer (org-scoped).

### Multi-Tenant ClickHouse: ConnectionRegistry (Design — not yet implemented)

**Decision:** dfe-engine manages multiple ClickHouse connections — one per role scope. Static connections for internal roles, auto-generated per-org connections with row-level security for customer isolation. dfe-engine creates CH users + row policies on org CRUD. Same connections pushed to HyperDX.

### HyperDX Integration (Design — not yet implemented)

**Decision:** dfe-engine is single source of truth for CH connections. Syncs to HyperDX via internal API (runtime) and `DEFAULT_CONNECTIONS` env var (bootstrap). One HyperDX team per customer org for isolation. Align with HyperDX patterns where they've made good choices (team API keys, connection model). Do NOT adopt HyperDX anti-patterns (no internal RBAC, no per-user scoping — we solve these at the dfe-engine layer).

### Rust Service Discovery: Schema-Less (Design — not yet implemented)

**Decision:** No typed plugin models for Rust services. Adding a new dfe-* service requires zero Python code. Service config surfaces defined in YAML (`config/service-surfaces/`). Metrics auto-discovered via rustlib `/metrics/manifest` endpoint. RBAC per-service (not per-metric or per-setting).

### Pylib Usage Policy (STRICT)

- **Logger:** `from hyperi_pylib.logger import logger` — NEVER stdlib `logging`
- **HTTP:** `HttpClient` / `AsyncHttpClient` from `hyperi_pylib.http` — NEVER raw `httpx`
- **Config store:** `DirectoryConfigStore` from `hyperi_pylib.config` — for all YAML registries
- **Deep merge:** `dfe_engine.yaml_utils.deep_merge()` (vendored) — pylib's `mergedeep` is file-level only
- **YAML:** `ruamel.yaml` via `dfe_engine.yaml_utils` (YAML 1.2) — intentional divergence from pylib's PyYAML 1.1
- **YAML 1.1 gotcha:** DirectoryConfigStore uses `yaml.safe_load` — `off`/`yes`/`no` become booleans. Don't store identity fields inside YAML; use filename as key.

---

## Module Status

| Module | Status | Notes |
| --- | --- | --- |
| `api/` | Active | 29 OpenAPI paths, 104 API tests. Phase 3 (hunts, queries, tasks) remaining |
| `source/` | Ready | TypeRegistry, Source, SourceRegistry, ExpressionValidator |
| `schema/` | Ready | v2 YAML→DDL pipeline, DDLFileWriter, ViewGenerator |
| `fieldmap/` | Ready | FieldMap model, registry, resolver, ViewGenerator |
| `clickhouse/` | Ready | clickhouse-connect client management |
| `pipeline/` | Ready | Vector pipeline generation |
| `sigma/` | Ready | SigmaSourceMapper + converter, FieldMapRegistry integration |
| `ai/` | Ready | AIModuleInterface ABC, 3 typed modules, AIModuleRegistry |
| `hunts/` | Ready | HuntEngine, Rule model, RuleCreationService, HdxSanitizer, Apprise alerts, alert grouping + cooldown |
| `query/` | Ready | YAML SSoT registry, Arrow-native output, built-in queries |
| `services/` | Ready | YAML SSoT registry, source routing, schema-less mode |
| `auth/` | Ready | Engine RBAC, open-ended argo: namespace, LocalAuthProvider |
| `helm/` | Ready | Values compiler, ExternalComponent, Argo CD app/project CRDs, RBAC, OTEL |
| `deployment/` | Ready | Pydantic models for K8s/KEDA, schema-less mode |
| `settings.py` | Ready | Pydantic config cascade + deepmerge + APISettings + FieldMapSettings |
| `yaml_utils.py` | Ready | Consolidated YAML operations |
| `storage/` | Ready | Local/HTTP/S3 storage backends (pylib HttpClient) |

---

## External Dependencies

- **hyperi-pylib[expression,http]>=2.25.0** — Logger, config, DirectoryConfigStore, HttpClient, HealthManager, CEL expressions
- **clickhouse-connect>=0.15.0** — ClickHouse client
- **fastapi>=0.135.1** — REST API framework
- **uvicorn[standard]>=0.41.0** — ASGI server
- **PyJWT[crypto]>=2.12.0** — JWT encode/decode (replaced python-jose)
- **pydantic>=2.12.5** — Model validation
- **ruamel-yaml>=0.19.1** — YAML parsing (1.2)
- **pysigma>=1.2.0** — Sigma rule conversion
- **apprise>=1.9.7** — Multi-channel notification dispatch
- **sse-starlette>=3.3.2** — Server-sent events for task streaming

---

## Resources

**Design Docs:**

- [docs/SOURCE.md](docs/SOURCE.md) — Source model design
- [docs/SCHEMA.md](docs/SCHEMA.md) — Schema v2 pipeline
- [docs/SYNC.md](docs/SYNC.md) — Config sync design
- [docs/EXPRESSIONS-CEL.md](docs/EXPRESSIONS-CEL.md) — CEL expression standard
- [docs/RBAC.md](docs/RBAC.md) — RBAC, multi-tenant ClickHouse, auth, Rust app layer design

**Infrastructure Research (WILL CHANGE — dfe-infra 2.1→2.2 port in progress):**

- [docs/dfe-infra.md](docs/dfe-infra.md) — DFE infrastructure deployment summary (dfe-infra project)
- [docs/stack-research.md](docs/stack-research.md) — DFE 2.2 stack decisions relevant to dfe-engine

**API:**

- `openapi-spec/openapi.json` — Committed OpenAPI spec (29 paths)
- `docker-compose.dev.yaml` — Prism mock server for frontend dev
