# TODO - DFE Engine

This is the **single source of truth** for all tasks and progress.

---

## Active Tasks

### RBAC Phase 1-4 (PR #16 — feat/rbac-phase1)

- [x] Phase 1: RBAC Foundation + Account CRUD (RoleConfig, stores, 4 auth paths, audit)
- [x] Phase 2: Connection Registry (multi-tenant CH, custom settings pattern)
- [x] Phase 3: Org Lifecycle + HyperDX (OrgRegistry, HyperDXClient)
- [x] Phase 4: Schema-Less Service Discovery (SurfaceRegistry)
- [x] Coverage push to 80.01% (2625 tests)

### OIDC Provider Registry (Phase 1.5 — feat/rbac-phase1)

- [x] Models: OIDCProvider, GroupInfo, GroupResolutionConfig
- [x] OIDCProviderRegistry (YAML-backed CRUD)
- [x] OIDCGroupAdapter ABC + GenericAdapter
- [x] source_provider field on Group model
- [x] Entra ID adapter (Graph API, MSAL client credentials)
- [x] Google adapter (Admin SDK, domain-wide delegation)
- [x] Okta adapter (stub — Okta uses token_claim mode)
- [x] Group sync runner (sync.py, async, upsert with role preservation)
- [x] REST API for provider CRUD (7 endpoints, 162 tests passing)
- [x] Control plane independence tests (529 lines)
- [ ] Final integration + push

### Pending

- [ ] CLI-API alignment: decide whether CLI calls stores directly or HTTP API
  - Current: CLI calls stores directly (works offline)
  - Future: CLI as HTTP client for remote operations
- [ ] Phase 3: Operational API routers (hunts, queries, tasks, pipeline)
- [ ] CI/CD: verify container + helm publish pipeline triggers correctly on next release

---

## Work Breakdown Structure (WBS)

### API Layer — Remaining Phases

**Goal:** Complete REST API surface to replace dfe-control-plane

#### Phase 3: Operational Routers

1. [ ] Hunts router (`v1/hunts.py`) — HuntEngine lifecycle, async execution (202 + polling)
2. [ ] Queries router (`v1/queries.py`) — ViewExecutor, absorb `query/endpoint.py`
3. [ ] Task manager (`api/task_manager.py`) — in-memory task store, polling + SSE
4. [ ] Pipeline router (`v1/pipeline.py`) — Vector pipeline generation

#### Phase 4: Discovery + Analytics

1. [ ] Discovery router (`v1/discovery.py`) — ClickHouse table/schema exploration
2. [ ] Sigma router (`v1/sigma.py`) — SigmaSourceMapper, converter
3. [ ] Schemas router (`v1/schemas.py`) — SchemaManager versions, DDL generation

#### Phase 5: UI Migration

1. [ ] Generate full OpenAPI spec, update `@repo/control-plane-types` in dfe-ui
2. [ ] Update dfe-ui API config to point at engine API
3. [ ] Create `docs/UI-API-GUIDE.md`

---

## Backlog

### High Priority

- [ ] **No-mock remediation** — migrate test suite from `unittest.mock` to real dependencies (testcontainers for ClickHouse, sandbox endpoints for apprise). See `STATE.md` for affected files. Do not add new mocks.
- [ ] Alert grouping integration tests — verify against real ClickHouse (DevEx cluster)
- [ ] `dfe-loader` transforms — replace hardcoded condition evaluation with rustlib CEL
- [ ] Standardise test infrastructure: dual-mode (remote devex + docker-local via dfe-docker infra profile). See `~/DFE-TEST-INFRA-PROMPT.md`

### Medium Priority

- [ ] Migrate DFE 2.1 deployment from `/projects/dfe-core` to DFE 2.2
- [ ] Create `dfe-config` repo with directory structure from DESIGN.md §2.2
- [ ] Seed default field maps (Sigma/ECS/CIM) as package resources
- [ ] Blob SHA ETag computation for conflict detection in DirectoryConfigStore writes
- [ ] Read-before-write pattern in DirectoryConfigStore writes
- [ ] Migrate Jinja2 templates from `INSERT INTO ... SELECT *` to lean `matched_uuid` pattern

### Low Priority

- [ ] **WASM transform module versioning** — registry, S3 storage, deploy/rollback API. Moved from dfe-transform-wasm Phase 7.4. See `docs/superpowers/specs/2026-03-19-wasm-module-versioning-wbs.md` for full WBS.
- [ ] Deploy HyperDX (ClickStack) as observability UI
- [ ] Deploy Envoy Gateway with native OIDC (replaces nginx-ingress + oauth2-proxy)
- [ ] OTEL metrics in Rust services (replace Prometheus)
- [ ] Integration tests using DevEx cluster (k8s-{1,2,3}.devex.hyperi.io)
- [ ] Cedar policy backend for auth (optional .cedar files + cedarpy)
- [ ] Increase test coverage to 90%+

---

## Design Docs

- `docs/RBAC.md` — RBAC, multi-tenant CH, auth, service discovery
- `docs/OIDC-PROVIDERS.md` — OIDC provider registry spec
- `docs/OIDC-INFRA-REQUIREMENTS.md` — what dfe-infra must provide for OIDC
- `docs/dfe-infra.md` — infrastructure research summary
- `docs/stack-research.md` — DFE 2.2 stack decisions

## Notes for AI Assistants

This file is the **single source of truth** for tasks and progress.

**Rules:**

- All tasks go here, nowhere else
- Mark tasks `[IN PROGRESS]` when starting
- Mark tasks `[x]` when complete
- Never add tasks to STATE.md or CLAUDE.md

**Project context:** See `STATE.md` for architecture and key decisions.
**Test run:** `uv run pytest` — uses project `.venv` (Python 3.12, managed by `uv`)
