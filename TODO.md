# DFE Engine - TODO

**Project Goal:** Production-ready core library for Data Fusion Engine

---

## Completed — Clean-Slate Restructure (2026-03-02)

- [x] Phase 1: Delete dead modules (config.py, targets/, data/) + skipped tests + phantom deps
- [x] Phase 2: Delete v1 schema code (6,427 lines) + v1 tests
- [x] Phase 3: Clean up sigma module — remove sqlalchemy/DB mode
- [x] Phase 4: Delete DFEConfigLoader + rewrite controllers to settings/Source
- [x] Phase 5: Flatten hunts/ structure (triple nesting → flat)
- [x] Phase 6: Add ExternalComponent model for two-mode Helm/Argo CD
- [x] Phase 7: Add proper `__init__.py` exports + fix package metadata + remove PostgresSettings
- [x] Phase 8: Final lint + test verification (1034 passed, 0 skipped)
- [x] Dependencies removed: deepdiff, gitpython, sqlparse, sqlalchemy, importlib-metadata

## Completed — Source + Schema v2 WBS

- [x] Phase 1: TypeRegistry, Source model, SourceRegistry (112 tests)
- [x] Phase 2: SchemaLoader, DDLGenerator, SchemaBuilderV2
- [x] Phase 3: SigmaSourceMapper, sigma_converter updated (24 tests)
- [x] Phase 4: Service routing for receiver + loader (17 tests)
- [x] Phase 5: v1 removal — deprecated/, schemas/, CSV resources
- [x] Phase 7: Cleanup — pythonpath fix, dependency update
- [x] Phase 8: Helm + Auth — HelmValuesCompiler, RBAC, auth module (71 tests)
- [x] Phase 8b: OTEL env config, KEDA scalers, Argo RBAC gen
- [x] Phase 8c: Argo CD Application + AppProject CRD generation

---

## Completed — DESIGN.md + Architecture Documentation

- [x] `/docs/DESIGN.md` created with mermaid diagrams covering:
  - GitOps config repo model (engine preferred but not exclusive, read-before-write, blob SHA ETag)
  - Git-native CRUD for UI (REST API, change preview, audit trail, conflict modal)
  - API layer architecture (FastAPI, generic CRUD router factory, engine stays library)
  - Field mapping layer (two-tier default + source-specific, YAML-managed, Sigma/ECS/CIM)
  - Deployment architecture (two-mode Helm/Argo CD, runtime data flow)
  - Source model integration diagram

---

## WBS: Field Mapping Layer (Sigma/ECS/CIM)

Generic standard-to-schema field mapping layer. Two-tier resolution (default table map + source-specific overrides). YAML-managed in config repo. See [DESIGN.md](docs/DESIGN.md) §5.

**Phase 1: Core field mapping models + resolver** — DONE (`8761e3e`)

- [x] `FieldMap` Pydantic model (standard, source, inherits, mappings dict)
- [x] `FieldMapRegistry` — DirectoryConfigStore-backed, loads from `field-maps/` directory
- [x] `resolve_field_map()` — two-tier resolution: source-specific > default > passthrough
- [x] Default table maps (`_default.yaml` per standard: Sigma, ECS, CIM)
- [x] 60 unit tests

**Phase 2+3: View DDL generation** — DONE (`6ec3491`)

- [x] `ViewGenerator` — generate `CREATE VIEW` DDL from resolved field maps
- [x] Generic `DDLGenerator.generate_view()` with configurable suffix
- [x] `SchemaBuildResult.view_ddls` for multi-standard view output
- [x] View naming: `{table_name}_{standard}` (e.g. `windows_audit_sigma`)
- [x] Drop view support (`generate_drop_view`)
- [x] 26 new tests (1120 total)

**Phase 4: Integration**

- [ ] Wire `ViewGenerator` into `SchemaBuilderV2.build()` (optional FieldMapRegistry)
- [ ] Source model: `mapping_standards` field (list of standards a source publishes views for)
- [ ] Sigma adapter: refactor `SigmaSourceMapper` to use FieldMapRegistry as primary source
- [ ] Schema compiler: populate `view_ddls` in build output

**Future (control-plane scope)**

- [ ] API model for field mapping CRUD (dfe-control-plane, not engine)
- [ ] Field mapping UI data model (standard fields + DFE columns + inheritance indicator)

---

## Remaining Source Model Work

**3.3 Hunts** (update `hunts/hunt.py`)

- [ ] Wire hunt rule loading to Source model (rule.source → SourceRegistry)
- [ ] Template variables: `{db}`, `{source}` resolved from Source
- [ ] Alerts table as a Source (timeseries profile)
- [ ] Update hunt validator for new schema format
- [ ] Unit tests

**7.4 Remove dfe_package.yaml dependencies** (remaining files)

- [ ] `sigma/field_mapping_service.py` → Source model
- [ ] `sigma/sigma_converter.py` → Source model
- [ ] `pipeline/pipeline_controller.py` → Source model
- [ ] `pipeline/pipeline_util.py` → Source model

---

## Config Repo Implementation (Planning)

See [DESIGN.md](docs/DESIGN.md) §2 for config repo structure and write model.

- [ ] Create `dfe-config` repo with directory structure from DESIGN.md §2.2
- [ ] Seed default field maps (Sigma/ECS/CIM) as package resources
- [ ] Implement blob SHA ETag computation for conflict detection
- [ ] Implement read-before-write pattern in DirectoryConfigStore writes
- [ ] Dev workflow: DevEx cluster config repo with default values
- [ ] AWS alternative: S3-backed config store with versioning

---

## Backlog

### Architecture (decided, not yet implemented)

- [ ] Deploy HyperDX (ClickStack) as observability UI
- [ ] Implement HyperDX OIDC middleware (docs/oauth2/HYPERDX-MIDDLEWARE.md)
- [ ] Deploy Envoy Gateway with native OIDC (replaces nginx-ingress + oauth2-proxy)
- [ ] OTEL metrics in Rust services (replace Prometheus)

### Near-Term

- [ ] Integration tests using DevEx cluster (k8s-{1,2,3}.devex.hyperi.io)
- [ ] More efficient hunt query spreading (EXPLAIN cost estimation, staggered scheduling)

### Future Enhancements

- [ ] Cedar policy backend for auth (optional .cedar files + cedarpy)
- [ ] Dev/test mode: auth disabled, root mode, seed defaults
- [ ] Async ClickHouse operations
- [ ] Schema diff visualization

### Technical Debt

- [ ] Increase test coverage to 90%+
- [ ] Add type hints to all public APIs

---

## Notes

- Use `uv` for all Python package management (NOT pip)
- Virtual environment: `~/.venv` (Python 3.12)
- See STATE.md for project status and architecture decisions
- Design docs: [DESIGN.md](docs/DESIGN.md), [SOURCE.md](docs/SOURCE.md), [SCHEMA.md](docs/SCHEMA.md), [SYNC.md](docs/SYNC.md)

---

**Last Updated:** 2026-03-02
