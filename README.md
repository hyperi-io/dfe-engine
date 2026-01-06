# DFE Engine

Core library for Data Fusion Engine - shared business logic for CLI, control plane, and other consumers.

## Installation

```bash
uv pip install dfe-engine
```

Or from source:

```bash
git clone https://github.com/hypersec-io/dfe-engine.git
cd dfe-engine
uv pip install -e ".[dev]"
```

## Quick Start

```python
from dfe_engine.settings import get_settings
from dfe_engine.schema import schema_builder

# Load configuration from environment
settings = get_settings()

# Access ClickHouse settings
print(f"ClickHouse: {settings.clickhouse.host}:{settings.clickhouse.port}")
```

## Architecture

```
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
              │          hs-pylib             │
              │   (Common Utilities)        │
              └─────────────────────────────┘
```

### Modules

| Module | Purpose |
|--------|---------|
| `clickhouse/` | ClickHouse connection management |
| `config/` | Configuration loading and target management |
| `schema/` | Schema creation, versioning, deployment to ClickHouse |
| `pipeline/` | Vector pipeline generation |
| `sigma/` | Sigma rule conversion to ClickHouse SQL |
| `hunts/` | Hunt scheduling and execution |
| `watcher_converter/` | Elastic Watcher to Hunt conversion |
| `opensearch/` | OpenSearch integration and templates |

## Development

### Prerequisites

- Python 3.12+
- [UV](https://docs.astral.sh/uv/) package manager
- Docker (for local databases)

### Setup

```bash
# Clone the repository
git clone https://github.com/hypersec-io/dfe-engine.git
cd dfe-engine

# Create virtual environment and install dependencies
uv venv
uv pip install -e ".[dev]"
```

### Local Databases

The project includes Docker Compose configuration for local ClickHouse and PostgreSQL:

```bash
# Start both databases
docker compose --profile test up -d

# Verify they're running
docker ps

# Check health
docker inspect -f '{{.State.Health.Status}}' dfe-clickhouse
docker inspect -f '{{.State.Health.Status}}' dfe-postgres
```

#### Profiles

| Profile | Services | Use Case |
|---------|----------|----------|
| `test` | ClickHouse + PostgreSQL | Running tests |
| `clickhouse` | ClickHouse only | Schema development |
| `postgres` | PostgreSQL only | Hunt checkpoint development |
| `all` | All services | Full local environment |

#### Customizing Versions

```bash
# Use specific database versions
export DFE_CLICKHOUSE_VERSION=24.8
export DFE_POSTGRES_VERSION=16
docker compose --profile test up -d
```

#### Data Persistence

Data is stored in Docker volumes. To reset:

```bash
docker compose down -v
```

### Running Tests

```bash
# All tests (parallel execution)
uv run pytest tests/ -v

# Unit tests only (no Docker required)
uv run pytest tests/unit_tests/ -v

# Specific test file
uv run pytest tests/unit_tests/test_schemas/test_schema_plan.py -v

# With coverage
uv run pytest tests/ -v --cov=src/dfe_engine --cov-report=term
```

### Using External Databases

To use external databases instead of Docker:

```bash
export DFE_CLICKHOUSE_HOST=your-clickhouse-server.example.com
export DFE_POSTGRES_HOST=your-postgres-server.example.com
```

When these are set to non-localhost values, Docker containers are not started.

## Configuration

All configuration uses environment variables with the `DFE_` prefix.

### ClickHouse

| Variable | Default | Description |
|----------|---------|-------------|
| `DFE_CLICKHOUSE_HOST` | `localhost` | Server host |
| `DFE_CLICKHOUSE_PORT` | `8123` | HTTP port |
| `DFE_CLICKHOUSE_NATIVE_PORT` | `9000` | Native protocol port |
| `DFE_CLICKHOUSE_USERNAME` | `default` | Username |
| `DFE_CLICKHOUSE_PASSWORD` | `` | Password |
| `DFE_CLICKHOUSE_DATABASE` | `default` | Default database |
| `DFE_CLICKHOUSE_SECURE` | `false` | Use HTTPS |
| `DFE_CLICKHOUSE_VERIFY` | `false` | Verify SSL certificates |

### PostgreSQL

| Variable | Default | Description |
|----------|---------|-------------|
| `DFE_POSTGRES_HOST` | `localhost` | Server host |
| `DFE_POSTGRES_PORT` | `5432` | Port |
| `DFE_POSTGRES_USER` | `postgres` | Username |
| `DFE_POSTGRES_PASSWORD` | `postgres` | Password |
| `DFE_POSTGRES_DATABASE` | `dfe_engine` | Database name |

### Kafka

| Variable | Default | Description |
|----------|---------|-------------|
| `DFE_KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Bootstrap servers |
| `DFE_KAFKA_SECURITY_PROTOCOL` | `PLAINTEXT` | Security protocol |

## Code Standards

### Logging

Use hs-pylib logger everywhere:

```python
from hs_pylib.logger import logger

logger.info("Processing started")
logger.error(f"Failed: {e}")
```

### Configuration

Use the settings module:

```python
from dfe_engine.settings import get_settings

settings = get_settings()
host = settings.clickhouse.host
```

### YAML Operations

Use yaml_utils:

```python
from dfe_engine.yaml_utils import yaml_load, yaml_dump

data = yaml_load(Path("config.yaml"))
yaml_dump(data, Path("output.yaml"))
```

## Related Projects

- [dfe-cli](https://github.com/hypersec-io/dfe-cli) - CLI wrapper
- [dfe-control-plane](https://github.com/hypersec-io/dfe-control-plane) - Control plane service
- [hs-pylib](https://github.com/hypersec-io/hs-pylib) - Common utilities

## License

Copyright (c) 2025 HyperSec. All rights reserved.

This is proprietary software. See [LICENSE](LICENSE) for details.
