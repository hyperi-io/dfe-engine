# Project State

**Project:** DFE Engine
**Purpose:** Core library for Data Fusion Engine - shared business logic for CLI, control plane, and other consumers
**Status:** Initial Setup

---

## Current Session (2025-12-05)

### Session Goals

- [x] Create dfe-engine repository
- [x] Attach CI and AI submodules
- [ ] Copy business logic modules from dfe-cli-core
- [ ] Apply remediation (hs-lib logger, security fixes)

### Progress

**Completed:**

- Repository created at github.com/hypersec-io/dfe-engine
- CI submodule attached with Python workflows
- AI submodule attached with Claude Code setup
- Package structure created (src/dfe_engine/)
- pyproject.toml configured with dependencies

**In Progress:**

- Migrating modules from dfe-cli-core

**Blocked:**

- None

---

## Project Overview

### Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                    Consuming Applications                        │
├────────────────────────────┬────────────────────────────────────┤
│       dfe-cli              │        dfe-control-plane           │
│   (Click CLI wrapper)      │     (Control Plane Service)        │
└─────────────┬──────────────┴──────────────┬─────────────────────┘
              │                             │
              └──────────────┬──────────────┘
                             ▼
              ┌─────────────────────────────┐
              │         dfe-engine          │  ← THIS REPO
              │   (Shared Library Package)  │
              └──────────────┬──────────────┘
                             │
                             ▼
              ┌─────────────────────────────┐
              │          hs-lib             │
              │   (Common Utilities)        │
              └─────────────────────────────┘
```

### Key Components

1. **clickhouse/** - ClickHouse connection management
2. **config/** - Configuration loading and validation
3. **schema/** - Schema creation, versioning, deployment
4. **pipeline/** - Vector pipeline generation
5. **opensearch/** - OpenSearch integration and templates
6. **sigma/** - Sigma rule conversion to ClickHouse
7. **hunts/** - Hunt scheduling and execution
8. **watcher_converter/** - Elastic Watcher conversion
9. **data_tools/** - Data utilities

### Tech Stack

- **Language:** Python 3.12+
- **Dependencies:** hs-lib (logging), pandas, clickhouse-driver, pysigma
- **Build:** Hatch/UV
- **Database:** ClickHouse, OpenSearch

---

## Migration Source

This library is being extracted from `dfe-cli-core` repository.

**Source modules (dfe-cli-core/src/dfecli/):**

| Source Module | Target Module |
|---------------|---------------|
| `dfe_clickhouse/` | `clickhouse/` |
| `dfe_config/` | `config/` |
| `dfe_schemabuilder/` | `schema/` |
| `dfe_pipelinebuilder/` | `pipeline/` |
| `dfe_opensearch/` | `opensearch/` |
| `dfe_sigma/` | `sigma/` |
| `dfe_async_hunts/` | `hunts/` |
| `dfe_elastic_watcher_converter/` | `watcher_converter/` |
| `data/data_tools/` | `data_tools/` |
| `resources/` | `resources/` |

---

## Remediation Required

### 1. Logging Migration

Replace all `DFELog` with `hs-lib` logger:

```python
# OLD
from dfecli.dfe_logger.dfe_logger import DFELog
log = DFELog.get_root_logger(...)

# NEW
from hs_lib.logger import logger
```

### 2. Security Fixes (Bandit)

| Issue | Fix |
|-------|-----|
| B113 | Add `timeout=30` to requests calls |
| B506 | Use `yaml.safe_load()` |
| B701 | Use `autoescape=True` in Jinja2 |
| B108 | Use `tempfile.mkdtemp()` |

### 3. Dependency Modernization

- Remove upper bounds on versions
- Use `>=` constraints

---

## Next Steps

**Immediate (this session):**

1. Copy modules from dfe-cli-core
2. Rename module paths (dfe_* → dfe_engine.*)
3. Replace DFELog with hs-lib logger

**Short-term:**

1. Fix security issues
2. Add tests
3. Verify >80% coverage
4. Initial publish to JFrog

**Long-term:**

1. Update dfe-cli to use dfe-engine
2. Update dfe-control-plane to use dfe-engine

---

## Resources

**Related Repositories:**

- [dfe-cli-core](https://github.com/hypersec-io/dfe-cli-core) - Source (being deprecated)
- [dfe-cli](https://github.com/hypersec-io/dfe-cli) - CLI consumer (to be created)
- [dfe-control-plane](https://github.com/hypersec-io/dfe-control-plane) - Control plane consumer
- [hs-lib](https://github.com/hypersec-io/hs-lib) - Common utilities

**Documentation:**

- [SCOPE.md in dfe-cli-core](../dfe-cli-core/SCOPE.md) - Full migration plan

---

**Last Updated:** 2025-12-05
**Version:** 0.1.0
**Status:** Initial Setup
