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

---

## Current Session (2026-03-02)

### Completed — Hunt Engine + Even Load Spreading

Replaced fragile `HuntScheduler` + `CronRunner` (os.fork()) architecture with `HuntEngine` — single daemon thread running an asyncio event loop.

**HuntEngine** (`hunts/hunt_engine.py`):
- `threading.Thread(daemon=True)` replaces `os.fork()` — visible to parent, cross-platform
- `start()`/`stop()`/`is_running` lifecycle with `threading.Event` + `asyncio.Event`
- Reads all config from `DFESettings.hunts` (no dfe_package.yaml dependency)
- `asyncio.wait_for()` replaces busy-loop timeout

**Even load spreading** (`compute_stagger_offsets()`):
- Even distribution formula: `offset_i = (F / N) * i` for N jobs over frequency F
- APScheduler `jitter` parameter for sub-minute randomization (default 15s)
- Handles per-minute, step-minute, fixed-minute, and long-interval cron expressions

**Settings expanded**: `HuntsSettings` gained `hunt_dir`, `rule_repo_dir`, `num_threads`, `checkpoint_destination`, `checkpoint_timestamp_field`, `jitter_seconds` with env var mappings.

**Deprecations**: `CronRunner.run()` (removed os.fork, added warning), `HuntScheduler` (added warning). Both remain importable.

**Test results:** 1163 passed, 0 failures (27 new tests: 15 stagger + 12 engine lifecycle)

### Completed — Field Mapping Layer (Phases 1-5)

Generic standard-to-DFE column mapping supporting Sigma, ECS, CIM and custom standards.

**Phase 1** (`8761e3e`): Core models + resolver
- `FieldMap` Pydantic model, `FieldMapRegistry` (DirectoryConfigStore-backed)
- `resolve_field_map()` — two-tier resolution (default + source-specific)
- Default seed maps for Sigma, ECS, CIM as package resources
- 60 tests

**Phase 2+3** (`6ec3491`): View DDL generation
- `ViewGenerator` — CREATE VIEW DDL from resolved field maps
- Generic `DDLGenerator.generate_view()` with configurable suffix
- `SchemaBuildResult.view_ddls` for multi-standard view output
- 26 tests

**Phase 4** (`6becc28`): Schema compiler integration
- `Source.mapping_standards` field for declaring view standards
- `SchemaBuilderV2` accepts optional `field_map_registry`, auto-generates views
- Legacy `sigma_view_ddl` preserved for backward compatibility
- 10 tests

**Phase 5** (`3976d08`): Sigma adapter refactoring
- `SigmaSourceMapper` accepts optional `field_map_registry`
- Two-tier registry resolution → fallback to `Source.sigma.custom_mappings`
- 6 tests

**Test results:** 1136 passed, 0 failures

### Completed — Clean-Slate Restructure + DESIGN.md

- 8-phase restructure: deleted legacy code, shims, tech debt (1034 tests)
- DESIGN.md with 11 mermaid diagrams (GitOps, CRUD, field mapping, deployment)

### Previous Sessions

- **2026-03-02 (earlier):** Field Mapping Layer (5 phases), clean-slate restructure, DESIGN.md (1136 tests)
- **2026-03-02 (earlier):** Phase 8c — Argo CD Application/AppProject CRD generator (1075 passed)
- **2026-03-01:** Phase 8b — OTEL env config, KEDA scalers, Argo RBAC generator (1026 tests)
- **2026-02-28:** Phases 1-8 — Source model, schema v2, sigma mapper, service routing, v1 removal, Helm compiler + RBAC (971 tests)

---

## Module Status

| Module        | Status | Notes                                                            |
| ------------- | ------ | ---------------------------------------------------------------- |
| `source/`     | Ready  | TypeRegistry, Source model, SourceRegistry — 112 tests           |
| `schema/`     | Ready  | v2 YAML→DDL pipeline, ViewGenerator integration                  |
| `fieldmap/`   | Ready  | FieldMap model, registry, resolver, ViewGenerator — 96 tests     |
| `clickhouse/` | Ready  | clickhouse-connect client management                             |
| `pipeline/`   | Ready  | Vector pipeline generation (core build/render logic)             |
| `sigma/`      | Ready  | SigmaSourceMapper + converter, FieldMapRegistry integration      |
| `hunts/`      | Ready  | HuntEngine daemon thread, even stagger, APScheduler jitter — 27 tests |
| `query/`      | Ready  | YAML SSoT registry, Arrow-native output, built-in queries        |
| `services/`   | Ready  | YAML SSoT registry, source routing, default configs, validators  |
| `auth/`       | Ready  | Engine RBAC, open-ended argo: namespace, infra_admin role        |
| `helm/`       | Ready  | Values compiler, ExternalComponent, Argo CD app/project CRDs, RBAC, OTEL |
| `deployment/` | Ready  | Pydantic models for K8s/KEDA, Prometheus + generic triggers      |
| `settings.py` | Ready  | Pydantic config cascade + deepmerge (no PostgresSettings)        |
| `yaml_utils.py` | Ready | Consolidated YAML operations                                   |
| `storage/`    | Ready  | Local/HTTP/S3 storage backends                                   |

### Removed Modules

| Module               | Removed In         | Reason                                  |
| -------------------- | ------------------ | --------------------------------------- |
| `config/`            | Restructure Ph 1+4 | Dead Config class + DFEConfigLoader shim → settings.py |
| `targets/`           | Restructure Ph 1   | Legacy Targets class → settings.py      |
| `data/`              | Restructure Ph 1   | Unused DataGenerator/DataTool           |
| `schema/v1 files`    | Restructure Ph 2   | 6,427 lines → v2 pipeline (949 lines)  |
| `deprecated/`        | Earlier Phase 5    | Replaced by settings.py + Source model  |
| `schemas/`           | Earlier Phase 5    | Replaced by schema/ v2 pipeline         |

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
- `deepmerge>=2.0` — Deep dict merging for settings and Helm values

### Development

- `pytest>=9.0.2` — Testing framework
- `pytest-xdist>=3.8.0` — Parallel test execution
- `ruff>=0.15.4` — Linting

---

**Last Updated:** 2026-03-02
