# TODO - DFE Engine

This is the **single source of truth** for all tasks and progress.

---

## Active Tasks

- [ ] Phase 3: Operational API routers `[PENDING]`
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
- [ ] Config repo decision: mono-repo vs separate repo for infra (TF/Helm/Argo)

### Medium Priority

- [ ] Migrate DFE 2.1 deployment from `/projects/dfe-core` to DFE 2.2
- [ ] Create `dfe-config` repo with directory structure from DESIGN.md §2.2
- [ ] Seed default field maps (Sigma/ECS/CIM) as package resources
- [ ] Blob SHA ETag computation for conflict detection in DirectoryConfigStore writes
- [ ] Read-before-write pattern in DirectoryConfigStore writes
- [ ] Migrate Jinja2 templates from `INSERT INTO ... SELECT *` to lean `matched_uuid` pattern

### Low Priority

- [ ] Deploy HyperDX (ClickStack) as observability UI
- [ ] Implement HyperDX OIDC middleware (docs/oauth2/HYPERDX-MIDDLEWARE.md)
- [ ] Deploy Envoy Gateway with native OIDC (replaces nginx-ingress + oauth2-proxy)
- [ ] OTEL metrics in Rust services (replace Prometheus)
- [ ] Integration tests using DevEx cluster (k8s-{1,2,3}.devex.hyperi.io)
- [ ] Cedar policy backend for auth (optional .cedar files + cedarpy)
- [ ] Increase test coverage to 90%+

---

## Notes for AI Assistants

This file is the **single source of truth** for tasks and progress.

**Rules:**

- All tasks go here, nowhere else
- Mark tasks `[IN PROGRESS]` when starting
- Mark tasks `[x]` when complete
- Never add tasks to STATE.md or CLAUDE.md

**Project context:** See `STATE.md` for architecture and key decisions.
**Test run:** `python -m pytest -q` — uses `~/.venv` (Python 3.12, managed by `uv`)
