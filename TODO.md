# DFE Engine - TODO

**Project Goal:** Production-ready core library for Data Fusion Engine

---

## Current Priority

### High Priority

- [ ] Commit all uncommitted work on `feat/rebrand-hyperi` branch
- [ ] Implement deployment config domain (t-shirt sizing, KEDA params, Helm values overrides)
- [ ] Implement OTEL metrics in Rust services (replace Prometheus scraping with push-based)

### Medium Priority

- [ ] Add SPDX-compliant headers to all source files
- [ ] Complete integration test coverage for schema module
- [ ] Document schema generation behavior (cityHash64, ORDER BY rules)
- [ ] Wire up ServicesSettings.config_yaml_dir to ServiceConfigRegistry initialization

### Low Priority

- [ ] Hunt scheduler smart query staggering (use EXPLAIN for cost estimation)
- [ ] Performance benchmarks for schema operations

---

## Recently Completed

- [x] Migrate `query/registry.py` from PostgreSQL to DirectoryConfigStore (YAML SSoT)
- [x] Create default service configs (dev + production) for receiver, loader, archiver
- [x] Add `seed_defaults()` to ServiceConfigRegistry
- [x] Clean all dfe-cli-core references from codebase
- [x] Rebase `feat/rebrand-hyperi` onto v1.3.71

---

## Backlog

### Architecture (decided, not yet implemented)

- [ ] Argo CD interface layer (optional — read sync status, health; dfe-engine writes config, Argo applies)
- [ ] Direct-apply fallback for environments without Argo CD
- [ ] Deploy HyperDX (ClickStack) as observability UI
- [ ] Implement HyperDX OIDC middleware (scoped in `docs/oauth2/HYPERDX-MIDDLEWARE.md`)
- [ ] Deploy Envoy Gateway with native OIDC (replaces nginx-ingress + oauth2-proxy)
- [ ] Vector ingest pipeline — reconcile dfe-engine core logic with control plane API/deployment

### Refactoring

- [ ] Extract KEDA controls into a dedicated module (out of deployment/sizing.py and deployment/models/)
- [ ] Extract Argo CD interface into a dedicated module (sync status, health checks, config apply)

### Future Enhancements

- [ ] Improve hunt query spreading/staggering (smarter scheduling to reduce ClickHouse load spikes)
- [ ] Storage abstraction layer (local, S3, HTTP)
- [ ] Async ClickHouse operations
- [ ] Schema diff visualization

### Technical Debt

- [ ] Increase test coverage to 90%+
- [ ] Add type hints to all public APIs
- [ ] Fix pre-existing ruff lint warnings in non-services modules
- [ ] Fix broken test imports in test_schemas and test_hunts (missing dfe_engine.config.config_loader)
- [ ] Add schema version support to dfe_package (schema_builder.py uses hardcoded version="1")

---

## Notes

- Use `uv` for all Python package management (NOT pip)
- Virtual environment: `.venv` (Python 3.12)
- Docker containers persist between test runs
- See STATE.md for current project status and architecture decisions

---

**Last Updated:** 2026-02-28
