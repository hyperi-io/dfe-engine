# DFE Engine - TODO

**Project Goal:** Production-ready core library for Data Fusion Engine
**Tests:** 1593 passing

---

## Engine Scope

### Hunts — Remaining

- [x] ~~Alert grouping + cooldown — read-time aggregation, per-(hunt,rule,customer) cooldown~~ (done 2026-03-03)
- [ ] Per-group cooldown — extend cooldown key to include group_by field values (v2)
- [ ] Alert grouping integration tests — verify against real ClickHouse (DevEx cluster)

### AI Module Interface (stub — implementation in separate repo)

Engine defines the interface contract. AI modules slot in from another project repo. Interface will iterate as engine evolves.

- [ ] `AIModuleInterface` ABC with three module types:
  - Query optimisation: takes query + execution profile → returns proposed query + rationale
  - Schema optimisation: takes source schema → returns proposed meta schema improvements
  - Parser: takes raw log samples → returns parsers + proposed meta schema
- [ ] Module discovery / registry (plugin pattern or entry_points)
- [ ] Async execution — AI modules run background tasks, return results asynchronously

### Config Repo + Deployment Migration

Needs architectural decision: TF + Helm + Argo under this project or separate. Must migrate from DFE 2.1 (dfe-core) AWS-only approach to DFE 2.2 engine.

- [ ] Decide: mono-repo vs separate repo for infra (TF/Helm/Argo)
- [ ] Migrate DFE 2.1 deployment approach from `/projects/dfe-core` to DFE 2.2
- [ ] Create `dfe-config` repo with directory structure from DESIGN.md §2.2
- [ ] Seed default field maps (Sigma/ECS/CIM) as package resources
- [ ] Blob SHA ETag computation for conflict detection
- [ ] Read-before-write pattern in DirectoryConfigStore writes
- [ ] Dev workflow: DevEx cluster config repo with default values
- [ ] AWS alternative: S3-backed config store with versioning

### Integration Tests

- [ ] Integration tests using DevEx cluster (k8s-{1,2,3}.devex.hyperi.io) — after above items complete

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

**Last Updated:** 2026-03-03
