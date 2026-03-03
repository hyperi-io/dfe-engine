# DFE Engine - Project State

**Project:** DFE Engine
**Version:** 1.5.0
**Status:** Active Development
**Branch:** `main`

---

## Architecture Decisions

### Config Storage: YAML SSoT (no PostgreSQL)

**Decision:** All configuration storage uses YAML directory as Single Source of Truth, backed by `DirectoryConfigStore` from hyperi-pylib. PostgreSQL is **not used** in dfe-engine — PG stays only in the control plane for RBAC (Casbin), audit logs, and sessions.

**Applies to:**

- Service configs (receiver, loader, archiver) — `ServiceConfigRegistry`
- Query definitions — `QueryRegistry`
- Source definitions — `SourceRegistry`
- Alert destinations — `AlertDestinationRegistry`

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

### Two-Mode Helm/Argo CD Management

**Decision:** Two deployment modes managed through the Helm compiler:

- **Mode 1 (DFE-managed):** Services built from engine Pydantic models with charts in our repo (receiver, loader, archiver, fetcher, transform-wasm)
- **Mode 2 (External):** Upstream charts with base values files + engine overrides deep-merged (vector.dev, kafbat, HyperDX, FerretDB)

**Implementation:** `ExternalComponent` model on `EnvironmentConfig` with `ChartSource` (repo_url, name, version), per-instance base values files, and `values_overrides` dict. Compiler generates Argo CD Application CRDs for both modes.

### Config Subrepo Pattern (Planned)

**Concept:** Shared git repo (or S3 bucket on AWS) as single config source. Engine is the PREFERRED management layer but NOT the only one — GitOps engineers can modify files directly and engine must cope with external changes.

**Status:** Architectural concept discussed, not yet implemented.

### Project Boundaries

**dfe-engine** is a pip-installable library. It owns business logic: config validation, query rendering, service config management, deployment config. It does NOT expose HTTP endpoints or handle auth — Casbin RBAC stays in dfe-control-plane where both API and CLI live.

**dfe-control-plane** is the FastAPI app + Typer CLI that imports dfe-engine and adds the HTTP/CLI layer (authentication, sessions, rate limiting, CORS).

**Rust services** (receiver, loader, archiver) read YAML config files directly from the config directory. No Python involved at runtime.

### Observability: OTEL replaces Prometheus

**Decision:** Swap Prometheus for OpenTelemetry. Metrics push to OTEL Collector, stored in ClickHouse, visualised in HyperDX (ClickStack).

**Status:** Implemented in Helm compiler — OTEL env vars injected into service values when `otel.enabled=True`.

### Argo CD RBAC: Open-Ended Action Namespace

**Decision:** Engine RBAC maps to Argo CD Casbin policies via open-ended `argo:<resource>:<action>` prefix — NOT a hardcoded lookup table.

### Testing Philosophy

**Decision:** dfe-engine is an integration layer. Unit tests use real Pydantic models and real logic (no mocks). Integration tests run against the DevEx cluster.

### Ingress: Envoy Gateway replaces nginx-ingress

**Decision:** nginx-ingress community EOL March 2026. Envoy Gateway has native OIDC via SecurityPolicy.

### Pylib Usage Policy

**Decision:** Engine MUST use hyperi-pylib primitives — no bespoke alternatives:

- **Logger:** `from hyperi_pylib.logger import logger` (loguru-based, NOT stdlib `logging`)
- **HTTP:** `HttpClient` / `AsyncHttpClient` from `hyperi_pylib.http` (retries, timeouts — NOT raw `httpx`)
- **Config store:** `DirectoryConfigStore` from `hyperi_pylib.config` for all YAML registries
- **Deep merge:** `deepmerge.always_merger` (separate pip package, pylib's `mergedeep` is file-level only)
- **YAML:** `ruamel.yaml` via `dfe_engine.yaml_utils` (YAML 1.2 — intentional divergence from pylib's PyYAML 1.1)

---

## Current Session (2026-03-03)

### Completed — Alert Grouping + Cooldown, Apprise Alerts, Pylib Audit

**Alert grouping + cooldown:** Read-time aggregation for hunt alerts. All rows written at full fidelity, grouping via post-INSERT `SELECT ... GROUP BY`. Cooldown per (hunt, rule, customer) via `dfe_audit.alert_state`.

**Apprise alert system:** `AlertConfig`, `AlertDispatcher`, `AlertDestinationRegistry` with DirectoryConfigStore backing.

**Pylib audit:** All violations fixed (logger, HTTP client, deep merge).

**Test results:** 1593 passed, 0 failures

### Previous Sessions

- **2026-03-02:** Hunt source wiring, Rule model, Expression validator, execution profiles, DDLFileWriter, adaptive scheduling + EXPLAIN capture
- **2026-03-01:** OTEL env config, KEDA scalers, Argo RBAC generator
- **2026-02-28:** Source model, schema v2, sigma mapper, service routing, v1 removal, Helm compiler + RBAC

---

## Module Status

| Module | Status | Notes |
| --- | --- | --- |
| `source/` | Ready | TypeRegistry, Source, SourceRegistry, ExpressionValidator — 163 tests |
| `schema/` | Ready | v2 YAML→DDL pipeline, DDLFileWriter, ViewGenerator integration |
| `fieldmap/` | Ready | FieldMap model, registry, resolver, ViewGenerator — 96 tests |
| `clickhouse/` | Ready | clickhouse-connect client management |
| `pipeline/` | Ready | Vector pipeline generation (core build/render logic) |
| `sigma/` | Ready | SigmaSourceMapper + converter, FieldMapRegistry integration |
| `hunts/` | Ready | HuntEngine, Rule model, source wiring, execution profiles, Apprise alerts, alert grouping + cooldown — 107 tests |
| `query/` | Ready | YAML SSoT registry, Arrow-native output, built-in queries |
| `services/` | Ready | YAML SSoT registry, source routing, default configs, validators |
| `auth/` | Ready | Engine RBAC, open-ended argo: namespace, infra_admin role |
| `helm/` | Ready | Values compiler, ExternalComponent, Argo CD app/project CRDs, RBAC, OTEL |
| `deployment/` | Ready | Pydantic models for K8s/KEDA, Prometheus + generic triggers |
| `settings.py` | Ready | Pydantic config cascade + deepmerge (no PostgresSettings) |
| `yaml_utils.py` | Ready | Consolidated YAML operations |
| `storage/` | Ready | Local/HTTP/S3 storage backends (pylib HttpClient) |

### Removed Modules

| Module | Removed In | Reason |
| --- | --- | --- |
| `config/` | Restructure Ph 1+4 | Dead Config class + DFEConfigLoader shim → settings.py |
| `targets/` | Restructure Ph 1 | Legacy Targets class → settings.py |
| `data/` | Restructure Ph 1 | Unused DataGenerator/DataTool |
| `schema/v1 files` | Restructure Ph 2 | 6,427 lines → v2 pipeline (949 lines) |
| `deprecated/` | Earlier Phase 5 | Replaced by settings.py + Source model |
| `schemas/` | Earlier Phase 5 | Replaced by schema/ v2 pipeline |

---

## Dependencies

### Runtime

- `hyperi-pylib>=2.19.0` — Logging, config, DirectoryConfigStore, HttpClient
- `clickhouse-connect>=0.13.0` — ClickHouse client
- `pyarrow>=23.0.1` — Arrow-native query output
- `pandas>=3.0.1` — DataFrame operations
- `numpy>=2.4.2` — Numerical operations
- `pydantic>=2.12.5` — Model validation
- `ruamel-yaml>=0.19.1` — YAML parsing
- `pysigma>=1.1.1` — Sigma rule conversion
- `httpx>=0.28.1` — Async HTTP client
- `deepmerge>=2.0` — Deep dict merging for settings and Helm values
- `apprise>=1.9.2` — Multi-channel notification dispatch

### Development

- `pytest>=9.0.2` — Testing framework
- `pytest-xdist>=3.8.0` — Parallel test execution
- `ruff>=0.15.4` — Linting

---

**Last Updated:** 2026-03-03
