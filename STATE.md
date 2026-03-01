# DFE Engine - Project State

**Project:** DFE Engine
**Version:** 1.4.0
**Status:** Active Development
**Branch:** `main` (uncommitted changes from WBS Phases 1-7)

---

## Architecture Decisions

### Config Storage: YAML SSoT (no PostgreSQL)

**Decision:** All configuration storage uses YAML directory as Single Source of Truth, backed by `DirectoryConfigStore` from hyperi-pylib. PostgreSQL is **not used** for config storage — PG stays only in the control plane for RBAC (Casbin), audit logs, and sessions.

**Applies to:**

- Service configs (receiver, loader, archiver) — `ServiceConfigRegistry`
- Query definitions — `QueryRegistry`
- Source definitions — `SourceRegistry`
- Future: targets, hunts, sigma rules, pipeline templates

**Rationale:** YAML files are directly readable by Rust services without any database dependency. Git-backed directories provide version history, branching, and audit trail. Both the control-plane API and a future CLI consume the same YAML tree.

**DirectoryConfigStore features used:**

- In-memory cache with background polling refresh
- Thread-safe reads via RLock
- Git-aware writes (auto-commit, branch management, push) via dulwich
- Change callbacks for reactive configuration
- Subdirectory support for table organization

### Source Model as Top-Level Data Abstraction

**Decision:** A `Source` is the top-level entity representing a data stream — replaces `dfe_package.yaml`, CSV type maps, and PG schema storage. Every data stream (e.g. `windows_audit`, `aws_cloudtrail`) is a Source YAML file in the SourceRegistry.

**Source owns:** match rules, schema (meta + derived + additional), sigma config, transform config, fetcher config, common header profile selection.

**Design docs:** [SOURCE.md](docs/SOURCE.md), [SCHEMA.md](docs/SCHEMA.md), [SYNC.md](docs/SYNC.md)

### Simplified Type System (13 Primitives)

**Decision:** Replace compound types (`string_fast`, `string_lowcardinality`, etc.) with 13 primitives: `string`, `text`, `integer`, `float`, `boolean`, `datetime`, `timestamp`, `date`, `ip`, `uuid`, `json`, `geo_point`, `enum`. Behaviour is controlled by `use_case` and `attribute` annotations on columns, resolved by `TypeRegistry`.

### Config Subrepo Pattern (Planned)

**Concept:** Shared git repo mounted as submodule by each component (engine, loader, receiver, hunts). Each component masters its own config domain; engine masters Source definitions. Components can operate in isolation with the config repo as a submodule, but all work together when run on the same machine.

**Status:** Architectural concept discussed, not yet implemented.

### Project Boundaries

**dfe-engine** is a pip-installable library. It owns business logic: config validation, query rendering, service config management, deployment config. It does NOT expose HTTP endpoints or handle auth — Casbin RBAC stays in dfe-control-plane where both API and CLI live.

**dfe-control-plane** is the FastAPI app + Typer CLI that imports dfe-engine and adds the HTTP/CLI layer (authentication, sessions, rate limiting, CORS).

**Rust services** (receiver, loader, archiver) read YAML config files directly from the config directory. No Python involved at runtime.

### Observability: OTEL replaces Prometheus

**Decision:** Swap Prometheus for OpenTelemetry. Metrics push to OTEL Collector, stored in ClickHouse, visualised in HyperDX (ClickStack).

**Status:** Implemented in Helm compiler — OTEL env vars injected into service values when `otel.enabled=True`. KEDA Prometheus scaler queries OTEL Collector's Prometheus-compatible endpoint (port 8889).

### Argo CD RBAC: Open-Ended Action Namespace

**Decision:** Engine RBAC maps to Argo CD Casbin policies via open-ended `argo:<resource>:<action>` prefix — NOT a hardcoded lookup table. Any `argo:*:*` permission is valid. If an Argo CD action matches, it works; if not, it warns and skips. Same open-ended approach for KEDA scalers via `KedaTriggerGeneric`.

**Rationale:** Argo CD's action surface is maintained by Argo CD upstream. Hardcoding a list requires manual maintenance. The prefix-match approach is self-documenting and future-proof.

### Testing Philosophy

**Decision:** dfe-engine is an integration layer. Mock-heavy unit tests have limited value because the engine's purpose is to orchestrate real infrastructure (ClickHouse, Kafka, Argo CD, KEDA, OTEL). Unit tests should use real Pydantic models and real logic. Integration tests should run against the DevEx cluster (k8s-{1,2,3}.devex.hyperi.io) with real services.

**Status:** Unit tests use real objects (no mocks). Integration test framework planned against DevEx environment.

### Ingress: Envoy Gateway replaces nginx-ingress

**Decision:** nginx-ingress community EOL March 2026. Envoy Gateway has native OIDC via SecurityPolicy, replacing both nginx-ingress AND oauth2-proxy.

**Status:** Spike config in `docs/oauth2/spike/envoy-gateway.yaml`.

---

## Current Session (2026-03-01)

### Completed — Phase 8b: Helm + Auth Enhancements

**deepmerge library** — replaced custom `_deep_merge()` in settings.py with `deepmerge.always_merger`

**OTEL environment config** — `OTelEnvironment` model in `helm/environment.py` with collector endpoint, protocol, prometheus_port, resource attributes. Injected into Helm values via compiler when `otel.enabled=True`.

**KEDA scalers** — `KedaTriggerPrometheus` for OTEL Collector metrics + `KedaTriggerGeneric` for any KEDA scaler type (open-ended, not hardcoded). Generic triggers support both service-component level (e.g. dfe-loader) and KEDA service level.

**infra_admin role** — new engine RBAC role for infrastructure engineers. Gets config:read/write, helm:*, and comprehensive `argo:*` actions. Open-ended `argo:<resource>:<action>` namespace — no hardcoded lookup table.

**Argo CD RBAC generator** — `helm/argo_rbac.py` generates `argocd-rbac-cm` Casbin policy CSV and AppProject `.spec.roles` from engine role definitions. Maps `argo:` permissions to Casbin lines, skips engine-only actions silently.

**Compiler updates** — OTEL env var injection, Prometheus trigger, generic trigger pass-through, `merge_overrides()` method using deepmerge, Argo RBAC generation in `compile_all()`, RBAC CSV output in `write_all()`.

**Cedar removed** — `.cedar` file support moved to TODO backlog. cedarpy was lazy-import only, no dependency to remove.

**Integration testing** — `.env.example` updated with DevEx cluster config (k8s-{1,2,3}.devex.hyperi.io, Argo CD, OTEL, ClickHouse, Kafka).

**Test results:** 1026 passed, 26 skipped, 0 errors, 0 failures

### Previous Session (2026-02-28)

Completed WBS Phases 1-7 (Foundation through Cleanup). 900 tests passing.

- Phase 1: TypeRegistry, Source model, SourceRegistry (112 tests)
- Phase 2: Schema v2 YAML→DDL pipeline
- Phase 3: Sigma + Hunts integration (24 tests)
- Phase 4: Service model updates (17 tests)
- Phase 5: v1 code removal (hard deletes)
- Phase 7: Cleanup — DFEConfigLoader shim, pythonpath fix, dependency update
- Phase 8: Helm Values Compiler + bespoke RBAC (71 tests, 971 total)

### Earlier Sessions

- hyperi-pylib updated to 2.19.0, ServiceConfigRegistry refactored (PG removed)
- Services module created with Pydantic models mirroring Rust config structs
- QueryRegistry migrated to YAML SSoT, default service configs created
- HyperDX OIDC middleware scoped in `docs/oauth2/`

---

## Module Status

| Module        | Status | Notes                                                            |
| ------------- | ------ | ---------------------------------------------------------------- |
| `source/`     | Ready  | TypeRegistry, Source model, SourceRegistry — 112 tests           |
| `schema/`     | Ready  | v2 YAML→DDL pipeline + v1 still functional                      |
| `clickhouse/` | Ready  | clickhouse-connect migration complete                            |
| `config/`     | Ready  | DFEConfigLoader shim delegating to settings.py                   |
| `pipeline/`   | Ready  | Vector pipeline generation (core build/render logic)             |
| `sigma/`      | Ready  | SigmaSourceMapper + converter + field mapping (lazy sqlalchemy)  |
| `hunts/`      | Ready  | Hunt scheduling                                                  |
| `query/`      | Ready  | YAML SSoT registry, Arrow-native output, built-in queries        |
| `services/`   | Ready  | YAML SSoT registry, source routing, default configs, validators  |
| `auth/`       | Ready  | Engine RBAC, open-ended argo: namespace, infra_admin role        |
| `helm/`       | Ready  | Values compiler, OTEL injection, Argo RBAC gen, merge_overrides  |
| `deployment/` | Ready  | Pydantic models for K8s/KEDA, Prometheus + generic triggers      |
| `settings.py` | Ready  | Pydantic config cascade + deepmerge                              |
| `yaml_utils.py` | Ready | Consolidated YAML operations                                   |

### Removed Modules

| Module               | Removed In | Reason                                  |
| -------------------- | ---------- | --------------------------------------- |
| `deprecated/`        | Phase 5    | Replaced by settings.py + Source model  |
| `schemas/`           | Phase 5    | Replaced by schema/ v2 pipeline         |
| `watcher_converter/` | Phase 5    | Legacy Elastic Watcher (in deprecated/) |
| `opensearch/`        | Phase 2    | No longer relevant                      |
| `dfe-data-resources/`| Phase 5    | Empty directory                         |

---

## Dependencies

### Runtime

- `hyperi-pylib>=2.19.0` — Logging, config, DirectoryConfigStore
- `clickhouse-connect>=0.13.0` — ClickHouse client
- `pyarrow>=23.0.1` — Arrow-native query output
- `pandas>=3.0.1` — DataFrame operations
- `numpy>=2.4.2` — Numerical operations
- `pydantic>=2.12.5` — Model validation
- `ruamel-yaml>=0.19.1` — YAML parsing
- `pysigma>=1.1.1` — Sigma rule conversion
- `httpx>=0.28.1` — Async HTTP client
- `sqlalchemy>=2.0.47` — PostgreSQL (query module only, lazy in sigma)
- `deepmerge>=2.0` — Deep dict merging for settings and Helm values

### Development

- `pytest>=9.0.2` — Testing framework
- `pytest-xdist>=3.8.0` — Parallel test execution
- `ruff>=0.15.4` — Linting
- `psycopg[binary]>=3.3.3` — PostgreSQL client (psycopg v3)

---

**Last Updated:** 2026-03-01
