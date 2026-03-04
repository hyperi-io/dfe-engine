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

### Pylib Usage Policy (STRICT)

- **Logger:** `from hyperi_pylib.logger import logger` — NEVER stdlib `logging`
- **HTTP:** `HttpClient` / `AsyncHttpClient` from `hyperi_pylib.http` — NEVER raw `httpx`
- **Config store:** `DirectoryConfigStore` from `hyperi_pylib.config` — for all YAML registries
- **Deep merge:** `deepmerge.always_merger` (pip package) — pylib's `mergedeep` is file-level only
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

- **hyperi-pylib[expression]>=2.19.0** — Logger, config, DirectoryConfigStore, HttpClient, CEL expressions
- **clickhouse-connect>=0.13.0** — ClickHouse client
- **fastapi>=0.115.0** — REST API framework
- **uvicorn[standard]>=0.34.0** — ASGI server
- **python-jose[cryptography]>=3.3.0** — JWT encode/decode
- **pydantic>=2.12.5** — Model validation
- **ruamel-yaml>=0.19.1** — YAML parsing (1.2)
- **pysigma>=1.1.1** — Sigma rule conversion
- **deepmerge>=2.0** — Deep dict merging
- **apprise>=1.9.2** — Multi-channel notification dispatch
- **sse-starlette>=2.0.0** — Server-sent events for task streaming

---

## Resources

**Design Docs:**

- [docs/SOURCE.md](docs/SOURCE.md) — Source model design
- [docs/SCHEMA.md](docs/SCHEMA.md) — Schema v2 pipeline
- [docs/SYNC.md](docs/SYNC.md) — Config sync design
- [docs/EXPRESSIONS-CEL.md](docs/EXPRESSIONS-CEL.md) — CEL expression standard

**API:**

- `openapi-spec/openapi.json` — Committed OpenAPI spec (29 paths)
- `docker-compose.dev.yaml` — Prism mock server for frontend dev
