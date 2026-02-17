# DFE Engine - TODO

**Project Goal:** Production-ready core library for Data Fusion Engine

---

## Current Priority

### High Priority

- [ ] Commit all uncommitted work on `feat/rebrand-hyperi` branch
- [ ] Migrate `query/registry.py` from PostgreSQL to DirectoryConfigStore (same pattern as services)
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

## Backlog

### Architecture (decided, not yet implemented)

- [ ] Deploy HyperDX (ClickStack) as observability UI
- [ ] Implement HyperDX OIDC middleware (scoped in `docs/oauth2/HYPERDX-MIDDLEWARE.md`)
- [ ] Deploy Envoy Gateway with native OIDC (replaces nginx-ingress + oauth2-proxy)
- [ ] Vector ingest pipeline — reconcile dfe-engine core logic with control plane API/deployment

### Future Enhancements

- [ ] Storage abstraction layer (local, S3, HTTP)
- [ ] Async ClickHouse operations
- [ ] Schema diff visualization

### Technical Debt

- [ ] Increase test coverage to 90%+
- [ ] Add type hints to all public APIs
- [ ] Fix pre-existing ruff lint warnings (12 issues in non-services modules)
- [ ] Add schema version support to dfe_package (schema_builder.py uses hardcoded version="1")
- [ ] Evaluate Liquibase alternative for schema versioning (schema_version_manager.py)

---

## Notes

- Use `uv` for all Python package management (NOT pip)
- Virtual environment: `~/.venv` (Python 3.12)
- Docker containers persist between test runs
- See STATE.md for current project status and architecture decisions

---

**Last Updated:** 2026-02-16
