# DFE Engine - Project State

**Project:** DFE Engine
**Version:** 1.3.71
**Status:** Active Development
**Branch:** `feat/rebrand-hyperi`

---

## Architecture Decisions

### Config Storage: YAML SSoT (no PostgreSQL)

**Decision:** All configuration storage uses YAML directory as Single Source of Truth, backed by `DirectoryConfigStore` from hyperi-pylib. PostgreSQL is **not used** for config storage — PG stays only in the control plane for RBAC (Casbin), audit logs, and sessions.

**Applies to:**
- Service configs (receiver, loader, archiver) — `ServiceConfigRegistry`
- Query definitions — `QueryRegistry`
- Future: targets, schemas, hunts, sigma rules, pipeline templates

**Rationale:** YAML files are directly readable by Rust services without any database dependency. Git-backed directories provide version history, branching, and audit trail. Both the control-plane API and a future CLI consume the same YAML tree.

**DirectoryConfigStore features used:**

- In-memory cache with background polling refresh
- Thread-safe reads via RLock
- Git-aware writes (auto-commit, branch management, push) via dulwich
- Change callbacks for reactive configuration
- Subdirectory support for table organization

### Project Boundaries

**dfe-engine** is a pip-installable library. It owns business logic: config validation, query rendering, service config management, deployment config. It does NOT expose HTTP endpoints or handle auth — Casbin RBAC stays in dfe-control-plane where both API and CLI live.

**dfe-control-plane** is the FastAPI app + Typer CLI that imports dfe-engine and adds the HTTP/CLI layer (authentication, sessions, rate limiting, CORS).

**Rust services** (receiver, loader, archiver) read YAML config files directly from the config directory. No Python involved at runtime.

### Observability: OTEL replaces Prometheus

**Decision:** Swap Prometheus for OpenTelemetry. Metrics push to OTEL Collector, stored in ClickHouse, visualised in HyperDX (ClickStack).

**Status:** Architectural decision made, not yet implemented in code.

### HyperDX OIDC Middleware

**Decision:** Minimal middleware patch for HyperDX to map OIDC group claims (from Envoy Gateway native OIDC) to ClickHouse connections. Supports Entra ID (GUIDs), Okta (display names), Google (email-based).

**Status:** Scoped and documented in `docs/oauth2/`. Spike code written. Not yet deployed.

### Ingress: Envoy Gateway replaces nginx-ingress

**Decision:** nginx-ingress community EOL March 2026. Envoy Gateway has native OIDC via SecurityPolicy, replacing both nginx-ingress AND oauth2-proxy.

**Status:** Spike config in `docs/oauth2/spike/envoy-gateway.yaml`.

---

## Current Session (2026-02-28)

### Completed

1. **Rebased onto v1.3.71** — resolved 8 merge conflicts from hs-pylib → hyperi-pylib rename

2. **QueryRegistry migrated to YAML SSoT** — PostgreSQL completely removed
   - `query/registry.py` rewritten to use DirectoryConfigStore (same pattern as ServiceConfigRegistry)
   - CRUD: `save_query()`, `delete_query()`, `get_query_history()`
   - Git passthrough: `is_git`, `current_branch`, `list_branches()`, `switch_branch()`
   - Built-in queries loaded from `query/builtin_queries/*.yaml` package resources
   - `load_from_file()` / `load_from_directory()` kept as import utilities for legacy format
   - `QuerySettings` added to `settings.py` with `DFE_QUERY_YAML_DIR` env var
   - 42 tests passing, ruff clean

3. **Default service configs created** — 6 YAML files in `services/default_configs/`
   - `receiver-default.yaml`, `receiver-production.yaml`
   - `loader-default.yaml`, `loader-production.yaml`
   - `archiver-default.yaml`, `archiver-production.yaml`
   - Dev configs: small buffers, debug logging, localhost connections
   - Production configs: K8s service DNS, SASL+TLS, tuned buffers, JSON logging
   - Production configs include K8s resource sizing and KEDA scaling comments
   - `seed_defaults()` method added to ServiceConfigRegistry
   - All configs validate through Pydantic models and round-trip through registry

4. **Cleaned dfe-cli-core references** — removed all references to the legacy 2.1 CLI
   - Updated SCOPE.md, hunts README, test fixtures, mock patches
   - CHANGELOG.md historical entry preserved (don't rewrite history)

### Previous Session (2026-02-16)

- hyperi-pylib updated from 2.18.0 to 2.19.0
- ServiceConfigRegistry refactored — PostgreSQL completely removed
- Services module docstrings updated to reflect YAML SSoT model
- Created `services/` module with Pydantic models mirroring Rust config structs
- Validators, templates (default/production/k8s profiles), state client
- `ServicesSettings` added to `settings.py` with env var support

---

## Module Status

| Module               | Status | Notes                                                         |
| -------------------- | ------ | ------------------------------------------------------------- |
| `clickhouse/`        | Ready  | clickhouse-connect migration complete                         |
| `config/`            | Ready  | Target management working                                     |
| `schema/`            | Ready  | All tests passing                                             |
| `pipeline/`          | Ready  | Vector pipeline generation (core build/render logic)          |
| `sigma/`             | Ready  | Sigma rule conversion                                         |
| `hunts/`             | Ready  | Hunt scheduling                                               |
| `watcher_converter/` | Ready  | Elastic Watcher conversion                                    |
| `opensearch/`        | Ready  | OpenSearch templates                                          |
| `query/`             | Ready  | YAML SSoT registry, Arrow-native output, built-in queries     |
| `services/`          | Ready  | YAML SSoT registry, default configs, state client, validators |
| `settings.py`        | Ready  | Pydantic config cascade, includes QuerySettings + ServicesSettings |
| `yaml_utils.py`      | Ready  | Consolidated YAML operations                                  |

---

## Dependencies

### Runtime

- `hyperi-pylib>=2.19.0` - Logging, config, DirectoryConfigStore
- `clickhouse-connect>=0.10.0` - ClickHouse client
- `pandas>=2.2.3` - DataFrame operations
- `ruamel-yaml>=0.18.6` - YAML parsing
- `pysigma>=1.0.2` - Sigma rule conversion
- `httpx>=0.28.1` - Async HTTP client (ServiceStateClient)

### Development

- `pytest>=8.3.0` - Testing framework
- `pytest-xdist>=3.5.0` - Parallel test execution
- `ruff>=0.8.0` - Linting

---

**Last Updated:** 2026-02-28
