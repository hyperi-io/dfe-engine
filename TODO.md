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

**Phase 4: Integration** — DONE (`6becc28`)

- [x] Wire `ViewGenerator` into `SchemaBuilderV2.build()` (optional FieldMapRegistry)
- [x] Source model: `mapping_standards` field (list of standards a source publishes views for)
- [x] Schema compiler: populate `view_ddls` in build output
- [x] 10 new tests (1130 total)

**Phase 5: Sigma adapter refactoring** — DONE (`3976d08`)

- [x] `SigmaSourceMapper` accepts optional `field_map_registry` parameter
- [x] Two-tier registry resolution with fallback to `Source.sigma.custom_mappings`
- [x] 6 new tests (1136 total)

**Future (control-plane scope)**

- [ ] API model for field mapping CRUD (dfe-control-plane, not engine)
- [ ] Field mapping UI data model (standard fields + DFE columns + inheritance indicator)

---

## Completed — Hunt Engine + Even Load Spreading (2026-03-02)

- [x] HuntsSettings expanded (hunt_dir, rule_repo_dir, checkpoint_destination, jitter_seconds, etc.)
- [x] `compute_stagger_offsets()` — even distribution algorithm with APScheduler jitter
- [x] `HuntEngine` class — daemon thread scheduler replacing os.fork() architecture
- [x] CronJob wired to new stagger, jitter passthrough to APScheduler
- [x] HuntController updated to use HuntEngine (no multiprocessing)
- [x] CronRunner + HuntScheduler deprecated with warnings
- [x] 27 new tests (15 stagger + 12 engine lifecycle), 1163 total passing

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

- [ ] Absorb dfe-control-plane into dfe-engine (REST API + CLI + logging + hunt management)
- [ ] Rules + Hunts workflow (see SQL-CLICKHOUSE.md § DFE Hunts):
  - [ ] Hunt results table schema (common header + lean detection columns: matched_uuid, rule_id, rule_name, source_table, hunt_name, severity)
  - [ ] Rule model — user-supplied SQL SELECT, time bounds stripped, output rewritten to `_uuid` + rule metadata only (never `SELECT *`)
  - [ ] Rule CRUD API (create from HyperDX SQL, validate, store as Jinja2 template)
  - [ ] Migrate existing Jinja2 templates from `INSERT INTO ... SELECT *` to lean `matched_uuid` pattern
  - [ ] Hunt results → source table join-back for full record lookup (UI concern)
- [ ] Hunt adaptive scheduling + backpressure:
  - [ ] **Overlap guard**: Skip/defer cron fire if previous execution of same `(customer, hunt, rule)` is still running — stretch the window using checkpoint instead of stacking parallel executions
  - [ ] **Adaptive interval**: When hunt takes longer than its cron interval, next execution uses the last checkpoint as window start (natural catch-up) — `REFRESH AFTER`-style semantics vs rigid cron
  - [ ] **Concurrency limit**: Max concurrent hunt queries per cluster (configurable), queue excess — prevents ClickHouse overload cascade
  - [ ] **Backpressure signal**: Expose hunt execution lag metric (scheduled_time - actual_start_time) via OTEL for alerting on cluster overload
  - [ ] Reference: [RunReveal checkpoint windowing](https://docs.runreveal.com/detections/detection-as-code), [ClickHouse RMV REFRESH AFTER](https://clickhouse.com/docs/materialized-view/refreshable-materialized-view)
- [ ] Hunt EXPLAIN + execution metrics logging:
  - [ ] **EXPLAIN output**: Run `EXPLAIN PLAN` (or `EXPLAIN pipeline`) before execution, log the query plan alongside the hunt execution record
  - [ ] **Execution profile**: After query completes, capture `read_rows`, `read_bytes`, `elapsed_ms`, `memory_usage` from ClickHouse query log (`system.query_log`) or client result profile
  - [ ] **Resource hog detection**: Flag hunts exceeding configurable thresholds (rows scanned, memory, duration) — log warning + optional metric
  - [ ] **AI query improvement**: Store execution profiles in structured format (JSON in checkpoint table or separate table) so LLM-based tools can analyse and suggest query optimisations (index usage, partition pruning, predicate pushdown)
  - [ ] **Query fingerprinting**: Normalise query text for grouping execution stats across runs (same rule, different time windows)
- [ ] Integration tests using DevEx cluster (k8s-{1,2,3}.devex.hyperi.io)

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
