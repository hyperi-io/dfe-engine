# DFE Engine - TODO

**Project Goal:** Production-ready core library for Data Fusion Engine
**Tests:** 1435 passing

---

## Engine Scope

### Hunts

- [ ] Concurrency limit — max concurrent hunt queries per cluster (configurable), queue excess
- [ ] Backpressure signal — expose hunt execution lag metric via OTEL
- [ ] Resource hog detection — flag hunts exceeding configurable thresholds (rows scanned, memory, duration)
- [ ] Query fingerprinting — normalise query text for grouping execution stats across runs
- [ ] AI query improvement — structured execution profiles for LLM-based query optimisation
- [ ] Alerts table as a Source (timeseries profile)
- [ ] Update hunt validator for new schema format

### Config Repo

See [DESIGN.md](docs/DESIGN.md) §2 for config repo structure and write model.

- [ ] Create `dfe-config` repo with directory structure from DESIGN.md §2.2
- [ ] Seed default field maps (Sigma/ECS/CIM) as package resources
- [ ] Blob SHA ETag computation for conflict detection
- [ ] Read-before-write pattern in DirectoryConfigStore writes
- [ ] Dev workflow: DevEx cluster config repo with default values
- [ ] AWS alternative: S3-backed config store with versioning

### Integration Tests

- [ ] Integration tests using DevEx cluster (k8s-{1,2,3}.devex.hyperi.io)

---

## Control-Plane Scope

- [ ] Absorb dfe-control-plane into dfe-engine (REST API + CLI + logging + hunt management)
- [ ] Rule CRUD API (create from HyperDX SQL, validate, store as Jinja2 template)
- [ ] Migrate Jinja2 templates from `INSERT INTO ... SELECT *` to lean `matched_uuid` pattern
- [ ] Hunt results → source table join-back for full record lookup (UI concern)
- [ ] Expression validator UI integration — endpoints for validation, field suggestion, expression preview
- [ ] Field mapping CRUD API + UI data model

---

## Backlog

### Architecture (decided, not yet implemented)

- [ ] Deploy HyperDX (ClickStack) as observability UI
- [ ] Implement HyperDX OIDC middleware (docs/oauth2/HYPERDX-MIDDLEWARE.md)
- [ ] Deploy Envoy Gateway with native OIDC (replaces nginx-ingress + oauth2-proxy)
- [ ] OTEL metrics in Rust services (replace Prometheus)

### Future

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
