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
- [x] Final integration + push (2792 tests, 80% coverage, pushed 2026-04-02)

### Pending

- [ ] CLI-API alignment: decide whether CLI calls stores directly or HTTP API
  - Current: CLI calls stores directly (works offline)
  - Future: CLI as HTTP client for remote operations
- [x] Phase 3: Operational API routers (hunts, queries, tasks, pipeline)
- [ ] CI/CD: verify container + helm publish pipeline triggers correctly on next release

---

## Work Breakdown Structure (WBS)

### API Layer — Remaining Phases

**Goal:** Complete REST API surface to replace dfe-control-plane

#### Phase 3: Operational Routers ✓

1. [x] Task manager (`api/task_manager.py`) — in-memory task store, polling + SSE
2. [x] Queries router (`v1/queries.py`) — ViewExecutor, absorb `query/endpoint.py`
3. [x] Hunts router (`v1/hunts.py`) — HuntEngine lifecycle, async execution (202 + polling)
4. [x] Pipeline router (`v1/pipeline.py`) — Vector pipeline generation

#### Phase 4: Discovery + Analytics ✓

1. [x] Discovery router (`v1/discovery.py`) — ClickHouse table/schema exploration
2. [x] Sigma router (`v1/sigma.py`) — SigmaSourceMapper, converter
3. [x] Schemas router (`v1/schemas.py`) — SchemaManager versions, DDL generation

#### Phase 5: UI Migration ✓

1. [x] Generate full OpenAPI spec (74 paths), fix generate.py for pylib compat
2. [x] Update dfe-ui API config — branch `feat/dereks-stab-at-engine-update` pushed
3. [x] Create `docs/UI-API-GUIDE.md` — auth, pagination, SSE, TypeScript types

#### Post-Phase: Review Fixes ✓

1. [x] Remove pyarrow — query module uses native JSON via clickhouse-connect
2. [x] Add 12 E2E workflow tests (auth, CRUD, task polling, consistency)
3. [x] Typed wrappers for schema-less GET endpoints (ServiceConfigDetail, DeploymentConfigDetail)
4. [x] Normalise path params ({hunt_name} → {name})
5. [x] Remove unauthenticated /queries/health endpoint

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

### High Priority (New)

- [ ] **JIT user provisioning + org CH isolation + HyperDX binding** — spec at `docs/superpowers/specs/2026-04-03-jit-provisioning-design.md`
  - Phase 1: OrgChProvisioner, OrgLifecycleManager, JitProvisioner, Account/Group/Org model changes
  - Phase 2: Core table/view management — schema deploy from dfe-schemas, idempotent, works with or without dfe-engine
  - Phase 3: SOC2 audit remediation — wire `audit_resource_change()` into ALL mutating API endpoints (sources, services, deployments, field maps, alerts, rules, OIDC, hunts, pipeline, schemas, transforms)
  - Phase 4: Real integration testing — Docker-based HyperDX (ClickStack) + ClickHouse for integration/E2E tests. No mocks/stubs/shims. Discuss: which other components need Docker test infra (Kafka? Envoy?)

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
