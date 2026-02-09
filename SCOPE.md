# Remediation Scope

**Project:** DFE Engine
**Purpose:** Track all remediation work required for the dfe-engine library
**Last Updated:** 2025-12-05

---

## Overview

This document tracks the remediation scope for migrating business logic from `dfe-cli-core` to `dfe-engine`. Remediation includes logging migration, configuration modernization, and security fixes.

---

## 1. Complete Logging Migration (Priority: HIGH)

**Goal:** Replace ALL logging patterns with `hyperi_pylib.logger`

### Target Pattern

```python
# STANDARD PATTERN - Use everywhere
from hyperi_pylib.logger import logger

logger.info("Message")
logger.error(f"Error: {e}")
logger.debug("Debug info")
```

### Files Requiring Changes

#### 1.1 Missing `import logging` (Runtime Errors)

| File | Line | Issue |
|------|------|-------|
| `data/data_tools/data_tool.py` | 10 | Uses `logging.getLogger()` without import |
| `config/config_loader.py` | 29 | Uses `logging.INFO` without import |
| `pipeline/pipeline_util.py` | 7 | Uses `logging.getLogger()` without import |
| `pipeline/pipeline.py` | 54 | Uses `logging.getLogger()` without import |
| `schema/schema_executor.py` | 40 | Uses `logging.getLogger()` without import |
| `opensearch/opensearch_tenancy.py` | 28 | Uses `logging.DEBUG` without import |
| `opensearch/apply_cm_templates.py` | 35 | Uses `logging.DEBUG` without import |
| `hunts/runner/cron_runner.py` | 27 | Type hint `logging.Logger` without import |

#### 1.2 Custom Logger Setup (Replace with hyperi-pylib)

| File | Lines | Current Pattern |
|------|-------|-----------------|
| `config/config_loader.py` | 12-29 | Custom `colorlog` setup with `StreamHandler` |
| `watcher_converter/watcher_converter.py` | 79-92 | `_setup_default_logger()` with custom formatter |
| `watcher_converter/hunt_generator.py` | 71-84 | `_setup_default_logger()` with custom formatter |

#### 1.3 Print Statements (Replace with logger)

| File | Line | Statement |
|------|------|-----------|
| `config/config_loader.py` | 254 | `print("Available targets:")` |
| `config/config_loader.py` | 256 | `print(f"- {target}")` |
| `config/config_loader.py` | 258 | `print("No targets found in configuration.")` |
| `hunts/hunts/hunts_controller.py` | 330 | `print(f"Error: {error}")` |
| `hunts/hunts/hunts_controller.py` | 424 | `print(f"Error: {error}")` |
| `hunts/hunts/hunts_controller.py` | 446 | `print(f"Log file not found: {hunt_log_file_path}")` |
| `pipeline/pipeline_controller.py` | 122 | `print("Attempting to load default DFE package configuration...")` |
| `pipeline/pipeline_controller.py` | 142 | `print("Attempting to load DFE package configuration...")` |
| `pipeline/pipeline.py` | 110 | `print("Loading Pipeline Template from", self.pipeline_template)` |
| `schema/schema_builder.py` | 399 | `print("Build process stopped due to critical schema validation errors...")` |
| `watcher_converter/watcher_parser.py` | 698 | `print(f"Warning: Missing or invalid 'action_transform_script'...")` |

#### 1.4 Direct logging Module Calls (Replace with hyperi-pylib)

| File | Lines | Current |
|------|-------|---------|
| `schema/schema_util.py` | 1015, 1018 | `logging.info()`, `logging.error()` without logger instance |
| `data/data_tools/data_tool.py` | 10-11 | `logging.getLogger(__name__)` + `setLevel()` |

#### 1.5 Already Migrated (No Changes Needed)

- `schema/schema_controller.py` - Uses `from hyperi_pylib.logger import logger`
- `opensearch/opensearch_apply.py` - Uses hyperi-pylib logger
- `sigma/sigma_converter.py` - Uses hyperi-pylib logger

---

## 2. Configuration Migration to hyperi-pylib (Priority: HIGH)

**Goal:** Replace all `os.getenv`/`os.environ` patterns with hyperi-pylib 7-level config cascade

### hyperi-pylib Config Cascade (Reference)

The hyperi-pylib configuration system implements an automatic 7-level cascade (highest to lowest priority):

| Priority | Source | Example | Use Case |
|----------|--------|---------|----------|
| 1 | CLI args | `--host=X` | Runtime override |
| 2 | ENV variables | `APP_DATABASE_HOST=x` | Deployment |
| 3 | `.env` file | Local secrets | Dev (gitignored) |
| 4 | `settings.{env}.yaml` | `settings.production.yaml` | Environment-specific |
| 5 | `settings.yaml` | Project config | Team defaults |
| 6 | `defaults.yaml` | Safe fallbacks | Local dev |
| 7 | Hard-coded | Code defaults | Last resort |

### Target Pattern

```python
# OLD - Direct environment access
host = os.getenv("DFE_CH_HOST")
port = os.getenv("DFE_CH_PORT", "9000")

# NEW - hyperi-pylib config cascade
from hyperi_pylib.config import settings

host = settings.get("clickhouse.host")
port = settings.get("clickhouse.port", 9000)

# Auto-generated ENV: APP_CLICKHOUSE_HOST, APP_CLICKHOUSE_PORT
```

### Environment Variables to Migrate

| Current Variable | File | Lines | Proposed Config Path |
|-----------------|------|-------|---------------------|
| `DFE_CH_HOST` | config_loader.py | 110, 185, 220, 285, 334 | `clickhouse.host` |
| `DFE_CH_PORT` | config_loader.py | 111, 186, 221 | `clickhouse.port` |
| `DFE_CH_USERNAME` | config_loader.py | 112, 187, 222 | `clickhouse.username` |
| `DFE_CH_PASSWORD` | config_loader.py | 113, 188, 223 | `clickhouse.password` |
| `DFE_CH_DATABASE` | config_loader.py | 114 | `clickhouse.database` |
| `CH_CONNECTIONS_MIN` | clickhouse_manager.py | 26 | `clickhouse.pool.min` |
| `CH_CONNECTIONS_MAX` | clickhouse_manager.py | 27 | `clickhouse.pool.max` |
| `ARTIFACTORY_VECTOR_TEMPLATES` | pipeline_controller.py | 202 | `artifactory.vector_templates_url` |
| `ARTIFACTORY_USERNAME` | pipeline_controller.py | 203 | `artifactory.username` |
| `ARTIFACTORY_PASSWORD` | pipeline_controller.py | 204 | `artifactory.password` |
| `TEMPLATES_VERSION` | pipeline_controller.py | 205 | `artifactory.templates_version` |
| `OPENSEARCH_DASHBOARDS_URL` | opensearch_dashboard_index_pattern.py | 192 | `opensearch.dashboards_url` |
| `HUNT_LOG_PATH` | cron_runner.py, hunts_controller.py | 43, 170, 320, 414, 553, 596 | `hunts.log_path` |

### Files Requiring Config Changes

| File | Lines | Current Pattern |
|------|-------|-----------------|
| `config/config_loader.py` | 51, 110-114, 185-188, 220-223, 285, 334, 443-444 | `os.getenv()`, `load_dotenv()` |
| `clickhouse/clickhouse_manager.py` | 26-27 | `os.environ.get()` |
| `pipeline/pipeline_controller.py` | 202-205 | `os.getenv()` |
| `pipeline/pipeline.py` | 218-219, 268-269 | `os.environ` dict access |
| `opensearch/opensearch_dashboard_index_pattern.py` | 192 | `os.getenv()` |
| `hunts/runner/cron_runner.py` | 43-44 | `os.getenv()` |
| `hunts/hunts/hunts_controller.py` | 170, 320, 414, 553, 596 | `os.getenv()` |

### Default Settings File Structure

Create `src/dfe_engine/defaults.yaml`:

```yaml
clickhouse:
  host: localhost
  port: 9000
  username: default
  password: ""
  database: default
  pool:
    min: 10
    max: 300

opensearch:
  dashboards_url: null

artifactory:
  vector_templates_url: null
  username: null
  password: null
  templates_version: latest

hunts:
  log_path: ./hunt_log_path
```

---

## 3. Security Fixes (Bandit)

### B113: Requests Without Timeout

**Status: NOT FIXED - 14 instances**

**Fix:** Add `timeout=30` to all requests calls.

#### opensearch/opensearch_apply.py (11 instances)

| Line | Call |
|------|------|
| 120 | `requests.get(url, headers=self.headers, auth=self.auth)` |
| 167 | `requests.get(url, headers=self.headers, auth=self.auth)` |
| 217 | `requests.get(base_url, headers=self.headers, auth=self.auth)` |
| 297 | `requests.put(base_url, headers=self.headers, json=payload, auth=self.auth)` |
| 322 | `requests.put(...)` |
| 346 | `requests.put(...)` |
| 364 | `requests.delete(base_url, headers=self.headers, auth=self.auth)` |
| 373 | `requests.put(...)` |
| 405 | `requests.put(...)` |
| 430 | `requests.put(...)` |
| 652 | `requests.get(url, headers=self.headers, auth=self.auth)` |

#### opensearch/opensearch_tenancy.py (1 instance)

| Line | Call |
|------|------|
| 45 | `requests.request(method, url, verify=verify, **kwargs)` |

#### opensearch/apply_cm_templates.py (3 instances)

| Line | Call |
|------|------|
| 154 | `requests.get(url, headers=..., auth=...)` |
| 201 | `requests.put(url, headers=..., auth=..., json=...)` |
| 249 | `requests.get(test_url, headers=..., auth=...)` |

### B506: YAML Safe Load

**Status: FIXED**

All instances use `yaml.safe_load()`.

### B701: Jinja2 Autoescape

**Status: NOT FIXED - 3 instances**

**Fix:** Add `autoescape=True` to all `Environment()` calls.

| File | Line | Current | Required |
|------|------|---------|----------|
| `pipeline/pipeline.py` | 116 | Missing parameter | Add `autoescape=True` |
| `schema/schema_executor.py` | 62 | `autoescape=False` | Change to `autoescape=True` |
| `hunts/runner/cron_runner.py` | 117 | Missing parameter | Add `autoescape=True` |

### B108: Hardcoded Temp Paths

**Status: FIXED**

Using proper `tempfile` module.

---

## 4. Module Status Summary ✅ ALL COMPLETE

| Module | Logging | Config | B113 | B701 | Status |
|--------|---------|--------|------|------|--------|
| clickhouse/ | ✅ | ✅ | N/A | N/A | **CLEAN** |
| config/ | ✅ | ✅ | N/A | N/A | **CLEAN** |
| data/ | ✅ | ✅ | N/A | N/A | **CLEAN** |
| opensearch/ | ✅ | ✅ | ✅ | N/A | **CLEAN** |
| pipeline/ | ✅ | ✅ | ✅ | ✅ | **CLEAN** |
| schema/ | ✅ | ✅ | N/A | ✅ | **CLEAN** |
| sigma/ | ✅ | ✅ | N/A | N/A | **CLEAN** |
| hunts/ | ✅ | ✅ | N/A | ✅ | **CLEAN** |
| watcher_converter/ | ✅ | ✅ | N/A | N/A | **CLEAN** |

---

## 5. Remediation Task Checklist

### Phase 1 - Logging (Critical) ✅ COMPLETE

- [x] Fix missing `import logging` in 8 files (runtime errors)
- [x] Replace `colorlog` setup in config_loader.py with hyperi-pylib
- [x] Replace custom `_setup_default_logger()` in watcher_converter (2 files)
- [x] Replace 11 print statements with logger calls
- [x] Replace direct `logging.info/error` calls in schema_util.py

### Phase 2 - Configuration ✅ COMPLETE

- [x] Create `defaults.yaml` with standard config structure
- [x] Migrate 12 environment variables to hyperi-pylib settings
- [x] Remove `load_dotenv()` calls (hyperi-pylib handles this)
- [x] Create settings.py with Pydantic-based config cascade
- [ ] Document new config paths for consumers

### Phase 3 - Security (Bandit) ✅ COMPLETE

- [x] Add `timeout=30` to 14 requests calls
- [x] Fix 3 Jinja2 autoescape issues

### Phase 4 - Dependency Remediation

- [x] Consolidate YAML libraries to use only `ruamel.yaml`
- [x] Remove PyYAML dependency (still transitive via hyperi-pylib/pysigma)
- [x] Migrate from `clickhouse-pool` + `clickhouse-driver` to `clickhouse-connect`
- [x] Update ClickHouseManager to use clickhouse-connect HTTP client with built-in pooling
- [x] Update data_tool.py to use clickhouse-connect
- [ ] Migrate from pandas to polars
- [ ] Update all DataFrame operations to polars API

### Phase 5 - Feature Enhancement (Future)

- [ ] Support additional Sigma rule sources beyond SigmaHQ repo and Valhalla API

---

## 6. Dependency Remediation (Priority: MEDIUM)

### 6.1 ClickHouse Package Migration

**Goal:** Use only ClickHouse Inc. official packages or packages based on them.

| Current Package | Maintainer | Status | Action |
|-----------------|------------|--------|--------|
| `clickhouse-connect>=0.10.0` | ClickHouse Inc. | ✅ Official | Keep |
| `clickhouse-driver>=0.2.10` | Konstantin Lebedev (Community) | ❌ Not official | Remove |
| `clickhouse-pool>=0.5.3` | Eric McCarthy (Community) | ❌ Not official, Inactive | Remove |

**Files Affected:**

| File | Current Usage | Migration |
|------|---------------|-----------|
| `clickhouse/clickhouse_manager.py` | `from clickhouse_pool import ChPool` | Use `clickhouse-connect` built-in pooling |
| `data/data_tools/data_tool.py` | `from clickhouse_driver import Client` | Use `clickhouse_connect.get_client()` |

**Connection Pooling Approach:**

`clickhouse-connect` has **built-in connection pooling** via urllib3. No external pooling library needed.

```python
# Default behavior - shared pool with 8 HTTP Keep-Alive connections
import clickhouse_connect
client = clickhouse_connect.get_client(host='localhost')

# Custom pool for high-throughput scenarios
from clickhouse_connect.driver import httputil

big_pool_mgr = httputil.get_pool_manager(maxsize=16, num_pools=12)
client = clickhouse_connect.get_client(pool_mgr=big_pool_mgr)
```

**Pool Configuration:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `maxsize` | 8 | Max connections per host |
| `num_pools` | 10 | Number of host connection pools |

**Migration Notes:**

- `clickhouse-connect` uses HTTP protocol (vs native TCP for clickhouse-driver)
- Built-in connection pooling via urllib3 - no external package needed
- Pool is shared across all client instances by default
- Custom pool can be configured for high-throughput scenarios

**Sources:**

- [clickhouse-connect PyPI](https://pypi.org/project/clickhouse-connect/) - Official ClickHouse Inc. driver
- [ClickHouse Python Advanced Usage](https://clickhouse.com/docs/integrations/language-clients/python/advanced-usage) - Connection pool docs
- [ClickHouse Python Docs](https://clickhouse.com/docs/integrations/python) - Official documentation

### 6.2 YAML Library Consolidation

**Goal:** Use a single YAML library - `ruamel.yaml` is recommended over `PyYAML`.

| Current Package | Status | Action |
|-----------------|--------|--------|
| `PyYAML>=6.0.3` | ❌ Legacy YAML 1.1 | Remove |
| `ruamel-yaml>=0.18.6` | ✅ Modern YAML 1.2 | Keep |

**Why ruamel.yaml:**

- Supports YAML 1.2 (current standard) vs PyYAML's YAML 1.1
- Preserves comments and formatting when editing
- Better boolean handling (no `yes`/`no` as booleans)
- Round-trip safe editing

**Sources:**

- [Why ruamel.yaml Should Be Your Python YAML Library](https://medium.com/top-python-libraries/why-ruamel-yaml-should-be-your-python-yaml-library-of-choice-81bc17891147)
- [ruamel.yaml PyPI](https://pypi.org/project/ruamel.yaml/)

### 6.3 DataFrame Library Migration (Pandas → Polars)

**Goal:** Replace pandas with polars for better performance and type safety.

| Current Package | Status | Replacement |
|-----------------|--------|-------------|
| `pandas>=2.3.3` | ❌ Legacy | `polars>=1.20.0` |
| `pyarrow>=22.0.0` | ✅ Keep | Required by polars |

**Benefits of Polars:**

- Significantly faster (Rust-based, lazy evaluation)
- Better memory efficiency
- Type-safe operations
- No GIL limitations

**Files Affected:**

| File | Current Usage |
|------|---------------|
| `hunts/hunts/hunts_controller.py` | `import pandas as pd`, `pd.DataFrame`, `pd.to_datetime` |
| `data/data_tools/data_tool.py` | Pandas DataFrame operations |
| `schema/schema_builder.py` | DataFrame for schema operations |

**Migration Pattern:**

```python
# OLD - Pandas
import pandas as pd
df = pd.DataFrame(data)
df["timestamp"] = pd.to_datetime(df["timestamp"])
df = df.sort_values(by="timestamp", ascending=False)

# NEW - Polars
import polars as pl
df = pl.DataFrame(data)
df = df.with_columns(pl.col("timestamp").str.to_datetime())
df = df.sort("timestamp", descending=True)
```

---

## 7. Verification

After remediation, verify with:

```bash
# Run bandit security scan
bandit -r src/dfe_engine/ -ll

# Check for remaining print statements
grep -r "print(" src/dfe_engine/ --include="*.py"

# Check for remaining os.getenv/os.environ
grep -rE "os\.(getenv|environ)" src/dfe_engine/ --include="*.py"

# Check for remaining logging imports
grep -r "import logging" src/dfe_engine/ --include="*.py"

# Run tests
pytest tests/ -v

# Check imports work
python -c "import dfe_engine"
```

---

## References

- [hyperi-pylib CONFIG.md](../hyperi-pylib/docs/CONFIG.md) - 7-level config cascade documentation
- [hyperi-pylib Logger](../hyperi-pylib/src/hyperi_pylib/logger/) - Logger module
- [Bandit B113](https://bandit.readthedocs.io/en/latest/plugins/b113_request_without_timeout.html) - Request without timeout
- [Bandit B701](https://bandit.readthedocs.io/en/latest/plugins/b701_jinja2_autoescape_false.html) - Jinja2 autoescape
