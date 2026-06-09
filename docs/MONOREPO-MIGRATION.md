# Monorepo Migration Guide

## Current State: Multi-Repo

Each DFE service has its own git repository:

| Service | Repository | Language |
|---------|-----------|----------|
| dfe-receiver | `hyperi-io/dfe-receiver` | Rust |
| dfe-loader | `hyperi-io/dfe-loader` | Rust |
| dfe-archiver | `hyperi-io/dfe-archiver` | Rust |
| dfe-transform-vector | `hyperi-io/dfe-transform-vector` | Rust |
| dfe-transform-wasm | `hyperi-io/dfe-transform-wasm` | Rust |
| dfe-fetcher | `hyperi-io/dfe-fetcher` | Rust |
| dfe-engine | `hyperi-io/dfe-engine` | Python |

## Plugin System (supports both models)

The service plugin system uses `importlib.metadata` entry points for discovery.
This means:

- **Multi-repo (current):** Each repo registers its plugin via its own `pyproject.toml`.
  External packages can extend the set of known services without touching dfe-engine.
- **Monorepo (future):** All plugins are registered from the single `pyproject.toml`.
  Built-in plugins already live in `dfe_engine.services.plugins_builtin`.

No code changes are needed for the migration — only `pyproject.toml` entry points.

### Entry Points (pyproject.toml)

```toml
[project.entry-points."dfe_engine.services"]
receiver = "dfe_engine.services.plugins_builtin.receiver:plugin"
loader = "dfe_engine.services.plugins_builtin.loader:plugin"
archiver = "dfe_engine.services.plugins_builtin.archiver:plugin"
transform-vector = "dfe_engine.services.plugins_builtin.transform_vector:plugin"
transform-wasm = "dfe_engine.services.plugins_builtin.transform_wasm:plugin"
fetcher = "dfe_engine.services.plugins_builtin.fetcher:plugin"
```

### Adding a New Service

1. Create config model in `src/dfe_engine/services/models/<service>.py`
2. Create deployment model in `src/dfe_engine/deployment/models/<service>.py`
3. Create plugin in `src/dfe_engine/services/plugins_builtin/<service>.py`
4. Add entry point to `pyproject.toml`
5. Add default configs to `default_configs/`
6. Run `uv pip install -e .` to refresh entry points

### External Plugin Registration (multi-repo)

An external package can register a service plugin:

```toml
# In the external package's pyproject.toml
[project.entry-points."dfe_engine.services"]
my-custom-service = "my_package.plugin:plugin"
```

The plugin function must return a `ServicePlugin` instance.

## Service Type Architecture

### Type 1 — Vanilla Services

Receiver, loader, archiver. Single flat config per deployment instance.
Standard Helm/ArgoCD env cascade.

### Type 2 — Multi-Source Services

Transform-wasm, fetcher. Vanilla base config plus CRUD-managed source
deployments. Each source has:

- **ENV overrides** — per-source environment variables
- **Associated files** — CSV, MMDB, JSON, WASM modules (via `SourceFileConfig`)
- **Enabled state** — can be toggled without deletion

Sources are managed via the engine API (called by the control plane REST API).

### Type 2+ — Vector Transform

Transform-vector is Type 2 plus an instance-specific config tree:

- Each source has a vector.dev YAML config file
- Sources can reference a parent (one YAML → multiple child transforms)
- The tree structure is specific to vector transforms (not reused elsewhere)

## Config Directory Layout

Single directory tree for all services:

```
<config_directory>/
    receiver-default.yaml
    receiver-production.yaml
    loader-default.yaml
    loader-staging.yaml
    archiver-default.yaml
    transform-vector-default.yaml
    transform-wasm-default.yaml
    fetcher-default.yaml
    ...
```

Naming: `{service}-{instance}.yaml`. Backed by `DirectoryConfigStore`.
May or may not be git-managed.

## Monorepo Directory Structure (proposed)

```
dfe/
├── engine/              # Python — dfe-engine
│   ├── src/dfe_engine/
│   ├── tests/
│   └── pyproject.toml
├── services/
│   ├── receiver/        # Rust — dfe-receiver
│   ├── loader/          # Rust — dfe-loader
│   ├── archiver/        # Rust — dfe-archiver
│   ├── transform-vector/# Rust — dfe-transform-vector
│   ├── transform-wasm/  # Rust — dfe-transform-wasm
│   └── fetcher/         # Rust — dfe-fetcher
├── helm/
│   ├── dfe-receiver/
│   ├── dfe-loader/
│   └── ...
└── ci/
    ├── build-rust.yaml
    └── build-python.yaml
```

The plugin system works identically in both layouts.
