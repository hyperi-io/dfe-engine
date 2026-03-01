# DFE Engine - TODO

**Project Goal:** Production-ready core library for Data Fusion Engine

---

## WBS: Source + Schema v2 Implementation

Design docs: [SOURCE.md](docs/SOURCE.md), [SCHEMA.md](docs/SCHEMA.md), [SYNC.md](docs/SYNC.md).

### Completed Phases

- **Phase 1: Foundation** — TypeRegistry, Source model, SourceRegistry (112 tests)
- **Phase 2: Schema v2** — SchemaLoader, DDLGenerator, SchemaBuilderV2
- **Phase 3: Sigma** — SigmaSourceMapper, sigma_converter updated, lazy sqlalchemy (24 tests)
- **Phase 4: Service Models** — source_routing for receiver + loader (17 tests)
- **Phase 5: v1 Removal** — deprecated/, schemas/, CSV resources, dead tests deleted
- **Phase 7: Cleanup** — DFEConfigLoader shim, pythonpath fix, test123 fix, dependency update
- **Phase 8: Helm + Auth** — HelmValuesCompiler, bespoke RBAC, auth module (71 tests, 971 total)
- **Phase 8b: Helm + Auth Enhancements** — Argo RBAC gen, OTEL env config, KEDA scalers, deepmerge, infra_admin role (1026 total)
- **Phase 8c: Argo CD App Generator** — Application + AppProject CRD generation, ArgoSyncPolicy/ArgoEnvironment models, compiler integration (1075 total)

### Remaining WBS Work

**3.3 Hunts** (update `hunts/hunts/hunts.py`)

- [ ] Wire hunt rule loading to Source model (rule.source → SourceRegistry)
- [ ] Template variables: `{db}`, `{source}` resolved from Source
- [ ] Alerts table as a Source (timeseries profile)
- [ ] Update hunt validator for new schema format
- [ ] Unit tests

**7.4 Remove dfe_package.yaml dependencies** (6 source files)

- [ ] `sigma/field_mapping_service.py` → Source model
- [ ] `sigma/sigma_converter.py` → Source model
- [ ] `schema/schema_controller.py` → Source model
- [ ] `hunts/hunts/hunts_controller.py` → Source model
- [ ] `pipeline/pipeline_controller.py` → Source model
- [ ] `pipeline/pipeline_util.py` → Source model

**7.5 Final lint pass**

- [ ] `ruff check src/` — clean
- [ ] `ruff check tests/` — clean
- [ ] Remove unused imports across codebase

### Verification Checklist

- [x] `pytest tests/unit/` — 1075 passed, 26 skipped, 0 errors, 0 failures
- [ ] `ruff check src/` — clean
- [ ] `grep -r "dfe_package" src/dfe_engine/` — zero results
- [x] `src/deprecated/` does not exist
- [x] `src/dfe_engine/schemas/` does not exist
- [x] `src/dfe_engine/dfe-data-resources/` does not exist

---

## Config Subrepo Design (Planning)

Shared git repo mounted as submodule by each component:

- [ ] Design repo structure (subdirs per component: engine/, loader/, receiver/, hunts/)
- [ ] Define write access model (each component masters its own domain)
- [ ] Source definitions mastered by engine, readable by all
- [ ] Dev workflow: components work in isolation via submodule, together on same machine
- [ ] Consider repurposing existing `dfe-data-resources` submodule

---

## Backlog (post-WBS)

### Architecture (decided, not yet implemented)

- [ ] Deploy HyperDX (ClickStack) as observability UI
- [ ] Implement HyperDX OIDC middleware (docs/oauth2/HYPERDX-MIDDLEWARE.md)
- [ ] Deploy Envoy Gateway with native OIDC (replaces nginx-ingress + oauth2-proxy)
- [ ] OTEL metrics in Rust services (replace Prometheus)
- [ ] Service properties from Helm chart with config cascade (concept)

### Near-Term

- [ ] Integration tests using DevEx cluster (k8s-{1,2,3}.devex.hyperi.io) — real Argo CD, KEDA, OTEL, ClickHouse, Kafka
- [ ] More efficient hunt query spreading (EXPLAIN cost estimation, staggered scheduling)

### Future Enhancements

- [ ] Cedar policy backend for auth (optional .cedar files + cedarpy, YAML stays default)
- [ ] Dev/test mode: auth disabled, root mode, seed defaults for dfe-docker compose
- [ ] Hunt query staggering (EXPLAIN cost estimation)
- [ ] Async ClickHouse operations
- [ ] Schema diff visualization
- [ ] Storage abstraction layer (local, S3, HTTP)

### Technical Debt

- [ ] Replace mock-heavy tests with integration tests (engine is an integration layer — mocks have low value)
- [ ] Increase test coverage to 90%+
- [ ] Add type hints to all public APIs
- [ ] SPDX-compliant headers on all source files

---

## Notes

- Use `uv` for all Python package management (NOT pip)
- Virtual environment: `~/.venv` (Python 3.12)
- `psycopg[binary]>=3.3.3` is psycopg v3 (async-capable, pure-Python + C-accelerated)
- See STATE.md for project status and architecture decisions
- Design docs: [SOURCE.md](docs/SOURCE.md), [SCHEMA.md](docs/SCHEMA.md), [SYNC.md](docs/SYNC.md)

---

**Last Updated:** 2026-03-02 (Phase 8c complete)
