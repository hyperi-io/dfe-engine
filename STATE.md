# DFE Engine - Project State

**Project:** DFE Engine
**Version:** 1.0.0
**Status:** Active Development

---

## Current State

### Completed Phases

1. **Repository Setup** - Repository created with CI/AI submodules
2. **Package Structure** - `src/dfe_engine/` with all core modules
3. **Logging Migration** - All files using hs-lib logger
4. **Settings Module** - Pydantic-based config with env cascade
5. **YAML Consolidation** - All YAML ops using ruamel.yaml via yaml_utils
6. **ClickHouse Migration** - Using clickhouse-connect with built-in pooling
7. **Security Fixes** - B113 (timeouts), B701 (Jinja2 autoescape)
8. **Docker Setup** - docker-compose.yml with profiles for dev/test
9. **Environment Variables** - DFE_ prefix for K8s compatibility
10. **psycopg3 Migration** - Updated from psycopg2 to psycopg3

### Test Status

| Test Suite | Passed | Failed | Notes |
|------------|--------|--------|-------|
| Schema Unit Tests | 67 | 19 | cityHash64 PRIMARY KEY tests need review |
| Pipeline Tests | ✓ | - | Passing |
| Sigma Tests | ✓ | - | Passing |

### Known Issues

1. **cityHash64 in PRIMARY KEY** - Schema generation adds `cityHash64(timestamp_load)` to PRIMARY KEY. Tests expect the original keys without the hash. Need to verify if this is intentional behavior or a regression.

---

## Module Status

| Module | Status | Notes |
|--------|--------|-------|
| `clickhouse/` | ✅ Ready | clickhouse-connect migration complete |
| `config/` | ✅ Ready | Target management working |
| `schema/` | ⚠️ Testing | 19 test failures to investigate |
| `pipeline/` | ✅ Ready | Vector pipeline generation |
| `sigma/` | ✅ Ready | Sigma rule conversion |
| `hunts/` | ✅ Ready | Hunt scheduling |
| `watcher_converter/` | ✅ Ready | Elastic Watcher conversion |
| `opensearch/` | ✅ Ready | OpenSearch templates |
| `settings.py` | ✅ Ready | Pydantic config cascade |
| `yaml_utils.py` | ✅ Ready | Consolidated YAML operations |

---

## Dependencies

### Runtime

- `hs-lib>=2.12.3` - Logging and common utilities
- `clickhouse-connect>=0.10.0` - ClickHouse client
- `pandas>=2.2.3` - DataFrame operations
- `ruamel-yaml>=0.18.6` - YAML parsing
- `pysigma>=1.0.2` - Sigma rule conversion
- `psycopg[binary]>=3.2.0` - PostgreSQL client

### Development

- `pytest>=8.3.0` - Testing framework
- `pytest-xdist>=3.5.0` - Parallel test execution
- `ruff>=0.8.0` - Linting

---

## Infrastructure

### Docker Services

```bash
# Start for development/testing
docker compose --profile test up -d

# Services
- dfe-clickhouse (ports 8123, 9000)
- dfe-postgres (port 5432)
```

### Environment Variables

All use `DFE_` prefix:
- `DFE_CLICKHOUSE_HOST`, `DFE_CLICKHOUSE_PORT`, etc.
- `DFE_POSTGRES_HOST`, `DFE_POSTGRES_PORT`, etc.

---

**Last Updated:** 2025-12-08
