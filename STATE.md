# DFE Engine - Project State

**Project:** DFE Engine
**Version:** 1.3.51
**Status:** Active Development
**Branch:** `feat/rebrand-hyperi`

---

## Architecture Decisions

### Config Storage: YAML SSoT (replaces PostgreSQL)

**Decision:** Service configuration (receiver, loader, archiver) uses YAML directory as Single Source of Truth, backed by `DirectoryConfigStore` from hyperi-pylib. PostgreSQL is **deprecated** for config storage.

**Rationale:** Removing moving parts. YAML files are directly readable by Rust services without any database dependency. Git-backed directories provide version history, branching, and audit trail. If the config directory is a git repo, changes are auto-committed.

**DirectoryConfigStore features used:**

- In-memory cache with background polling refresh
- Thread-safe reads via RLock
- Git-aware writes (auto-commit, branch management, push) via dulwich
- Change callbacks for reactive configuration
- Subdirectory support for table organization

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

## Current Session (2026-02-16)

### Completed

1. **hyperi-pylib updated** from 2.18.0 to 2.19.0 (from JFrog)
   - pyproject.toml minimum bumped to `>=2.19.0`
   - DirectoryConfigStore now uses dulwich (pure-Python git) instead of system git binary
   - Supports subdirectory table names (e.g. `loaders/dfe-loader`)

2. **ServiceConfigRegistry refactored** — PostgreSQL completely removed
   - `src/dfe_engine/services/registry.py` rewritten (608 → 460 lines)
   - Backed by `DirectoryConfigStore` from hyperi-pylib
   - Directory layout: `{service}-{instance}.yaml` files
   - History via dulwich git log walker (replaces PG history table)
   - Git operations: delete via `dulwich.porcelain.rm`, commits via dulwich
   - Git passthrough: `is_git`, `current_branch`, `list_branches()`, `switch_branch()`
   - Change callbacks via `on_change(service, instance, callback)`
   - All smoke tests passing (CRUD + singleton + round-trip for all 3 services)
   - Updated for hyperi-pylib 2.19.0 dulwich migration (no more subprocess git)

3. **Services module docstrings** updated to reflect YAML SSoT model

### Previous Session Work (pre-compaction)

- Created `services/` module with Pydantic models mirroring Rust config structs
- Models: `ReceiverConfig`, `LoaderConfig`, `ArchiverConfig` + all sub-models
- Validators, templates (default/production/k8s profiles), state client
- `ServicesSettings` added to `settings.py` with env var support
- HyperDX OIDC middleware scope documented in `docs/oauth2/`
- Spike configs: envoy-gateway.yaml, nginx.conf, oauth2-proxy.cfg, k8s-ingress.yaml

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
| `query/`             | Ready  | ClickHouse queries with PG+YAML registry                      |
| `services/`          | Ready  | Config registry (YAML SSoT), state client, models, validators |
| `settings.py`        | Ready  | Pydantic config cascade, includes ServicesSettings            |
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
- `psycopg[binary]>=3.2.0` - PostgreSQL client (query module only)

---

## Uncommitted Changes

All on branch `feat/rebrand-hyperi`:

- `pyproject.toml` — hyperi-pylib version bump
- `src/dfe_engine/settings.py` — ServicesSettings added
- `src/dfe_engine/services/` — entire new module (untracked)
- `docs/oauth2/` — OIDC middleware docs and spike configs (untracked)

---

**Last Updated:** 2026-02-16
