# Query Gateway API

**Safe, RBAC-controlled query interface for PostgreSQL, ClickHouse, Prometheus, and Kafka**

---

## Overview

The Query Gateway provides a unified, secure interface for executing queries across multiple data stores without requiring a dedicated API endpoint per query. It implements:

- **Query Registry**: YAML-defined query templates with parameterisation
- **RBAC**: Casbin-based role access control per query
- **Tenant Isolation**: Mandatory org_id scoping injected server-side
- **Cost Estimation**: Pre-execution EXPLAIN for ClickHouse
- **Caching**: PostgreSQL-backed shared cache for ClickHouse, Prometheus, and Kafka
- **Audit Logging**: Full query execution audit trail

---

## Unified API Design

### Single Endpoint, Multiple Stores

All stores are accessed via one API with store-specific routing:

```
POST /api/query
{
  "query_id": "kafka.topic_messages",  // store prefix determines routing
  "params": {...}
}
```

The `query_id` format is `{namespace}.{operation}` where namespace maps to store:
- `analytics.*`, `hunts.*` → ClickHouse
- `users.*`, `config.*` → PostgreSQL
- `metrics.*` → Prometheus
- `kafka.*`, `streams.*` → Kafka

### Pros of Unified API

| Advantage | Description |
|-----------|-------------|
| **Single integration point** | Frontend learns one API, not four |
| **Consistent RBAC** | Same Casbin policy model for all stores |
| **Unified audit** | Single log format for all query types |
| **Shared infrastructure** | Cache, rate limiting, auth middleware reused |
| **Simpler SDK** | One TypeScript client class |
| **Easier testing** | Mock one endpoint, test all stores |

### Cons of Unified API

| Disadvantage | Mitigation |
|--------------|------------|
| **Store-specific features hidden** | Expose via `options` field (e.g., `explain` for CH) |
| **Different response shapes** | Normalise to common `{data, meta}` structure |
| **Kafka is fundamentally different** | Read-only operations fit the pattern; streaming doesn't |
| **Error handling varies** | Map to common error codes with store-specific details |
| **Performance characteristics differ** | Per-store timeouts and limits in query definitions |

### Recommendation

**Use unified API.** The benefits outweigh the complexity. Kafka fits because:
1. We only expose read operations (topic metadata, message sampling, lag)
2. hs-pylib already provides a read-only Kafka client (`ReadOnlyKafkaClient`)
3. Streaming/subscription is a separate concern (WebSocket, not REST)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        TypeScript Frontend                               │
└─────────────────────────────────────────────────┬───────────────────────┘
                                                  │
                                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        dfe-control-plane                                 │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────┐  │
│  │  AuthN (JWT)    │  │  AuditLogger    │  │  Rate Limiter           │  │
│  └────────┬────────┘  └────────┬────────┘  └────────────┬────────────┘  │
│           │                    │                        │               │
│           └────────────────────┼────────────────────────┘               │
│                                ▼                                        │
│                    POST /api/query                                      │
│                    { query_id, params }                                 │
└─────────────────────────────────────────────────┬───────────────────────┘
                                                  │
                                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          dfe-engine                                      │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                     query_gateway/                               │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │    │
│  │  │  Registry   │  │  Validator  │  │  RBAC Enforcer (Casbin) │  │    │
│  │  │  (YAML)     │  │  (Params)   │  │                         │  │    │
│  │  └──────┬──────┘  └──────┬──────┘  └────────────┬────────────┘  │    │
│  │         │                │                      │               │    │
│  │         └────────────────┼──────────────────────┘               │    │
│  │                          ▼                                      │    │
│  │  ┌─────────────────────────────────────────────────────────┐   │    │
│  │  │              QueryRenderer (Jinja2 + typed params)       │   │    │
│  │  │              + Mandatory org_id injection                │   │    │
│  │  └─────────────────────────────────────────────────────────┘   │    │
│  │                          │                                      │    │
│  │         ┌────────────────┼────────────────────────┐            │    │
│  │         ▼                ▼                ▼       ▼            │    │
│  │  ┌───────────┐   ┌───────────┐    ┌──────────┐ ┌──────────┐   │    │
│  │  │  CH Exec  │   │  PG Exec  │    │Prom Exec │ │Kafka Exec│   │    │
│  │  │ + EXPLAIN │   │           │    │          │ │(hs-pylib)│   │    │
│  │  └─────┬─────┘   └─────┬─────┘    └────┬─────┘ └────┬─────┘   │    │
│  │        │               │               │            │         │    │
│  └────────┼───────────────┼───────────────┼────────────┼─────────┘    │
│           │               │               │            │              │
│  ┌────────┴───────────────┴───────────────┴────────────┴──────────┐   │
│  │                    PostgresCache (shared)                       │   │
│  │                 (CH + Prometheus + Kafka metadata)              │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┬──────────────────────┘
                                                  │
         ┌────────────────┬───────────────┬───────┴───────┐
         ▼                ▼               ▼               ▼
  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
  │ ClickHouse  │  │ PostgreSQL  │  │ Prometheus  │  │    Kafka    │
  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘
```

---

## RBAC Design

### Casbin Model

We use RBAC with domains (multi-tenant) and resource-based permissions. The model allows:

- Users have roles within organisations (domains)
- Roles grant access to specific query IDs
- Query definitions specify which roles can execute them

**Model Configuration** (`model.conf`):

```ini
[request_definition]
r = sub, dom, obj, act

[policy_definition]
p = sub, dom, obj, act

[role_definition]
g = _, _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub, r.dom) && r.dom == p.dom && r.obj == p.obj && r.act == p.act
```

**Components:**

| Element | Description | Example |
|---------|-------------|---------|
| `sub` | Subject (user or role) | `user:alice`, `role:analyst` |
| `dom` | Domain (organisation) | `org:acme-corp` |
| `obj` | Object (query ID) | `analytics.events_by_day` |
| `act` | Action | `execute`, `explain` |

### Policy Storage

Policies stored in PostgreSQL using [casbin-async-sqlalchemy-adapter](https://github.com/pycasbin/async-sqlalchemy-adapter):

```python
from casbin import AsyncEnforcer
from casbin_async_sqlalchemy_adapter import Adapter

adapter = Adapter("postgresql+asyncpg://user:pass@localhost/dfe")
enforcer = AsyncEnforcer("model.conf", adapter)
```

### Policy Examples

```csv
# Roles
p, role:admin, org:*, *, execute
p, role:admin, org:*, *, explain
p, role:analyst, org:*, analytics.*, execute
p, role:analyst, org:*, analytics.*, explain
p, role:viewer, org:*, analytics.events_summary, execute
p, role:hunt_operator, org:*, hunts.*, execute

# Role assignments (user, role, domain)
g, user:alice, role:admin, org:acme-corp
g, user:bob, role:analyst, org:acme-corp
g, user:charlie, role:viewer, org:acme-corp
```

### Query-Level RBAC in Registry

Each query definition specifies allowed roles:

```yaml
queries:
  events_by_day:
    store: clickhouse
    description: Daily event counts

    # RBAC: roles that can execute this query
    roles:
      - admin
      - analyst
      - viewer  # Read-only access

    # Optional: different roles for explain vs execute
    roles_explain:
      - admin
      - analyst

    params:
      org_id:
        type: string
        scope: tenant
      # ...
```

### Enforcement Flow

```python
async def execute_query(
    principal: str,      # e.g., "user:alice"
    org_id: str,         # e.g., "acme-corp"
    query_id: str,       # e.g., "analytics.events_by_day"
    params: dict,
    explain: bool = False,
) -> QueryResult:

    # 1. Load query definition
    query_def = registry.get(query_id)
    if not query_def:
        raise QueryNotFoundError(query_id)

    # 2. RBAC enforcement
    action = "explain" if explain else "execute"
    domain = f"org:{org_id}"

    allowed = await enforcer.enforce(principal, domain, query_id, action)
    if not allowed:
        logger.warning("RBAC denied", principal=principal, org=org_id, query=query_id)
        raise PermissionDeniedError(f"Access denied to {query_id}")

    # 3. Validate and inject org_id
    validated_params = validator.validate(query_def, params)
    validated_params["org_id"] = org_id  # Mandatory injection

    # 4. Execute
    return await executor.execute(query_def, validated_params, explain=explain)
```

---

## Query Registry

### Directory Structure

```
src/dfe_engine/query_gateway/
├── __init__.py
├── controller.py          # SafeQueryController (static methods)
├── registry.py            # QueryRegistry loader
├── validator.py           # Parameter validation
├── renderer.py            # SQL/PromQL rendering with org injection
├── enforcer.py            # Casbin RBAC integration
├── cache.py               # PostgresCache backend
├── guards.py              # Cost estimation, limits
├── model.conf             # Casbin model definition
├── executors/
│   ├── __init__.py
│   ├── base.py            # BaseExecutor protocol
│   ├── clickhouse.py      # ClickHouse executor + EXPLAIN
│   ├── postgres.py        # PostgreSQL executor
│   └── prometheus.py      # Prometheus executor
└── queries/               # Query definitions
    ├── clickhouse/
    │   ├── analytics.yaml
    │   └── hunts.yaml
    ├── postgres/
    │   └── users.yaml
    └── prometheus/
        └── metrics.yaml
```

### Query Definition Schema

```yaml
# queries/clickhouse/analytics.yaml
queries:
  events_by_org_by_day:
    store: clickhouse
    description: Daily event counts by organisation

    # RBAC roles allowed to execute
    roles:
      - admin
      - analyst

    # Parameter definitions
    params:
      org_id:
        type: string
        required: true
        scope: tenant          # Mandatory tenant filter
      start_time:
        type: datetime
        required: true
        validate: "value <= utcnow()"
      end_time:
        type: datetime
        required: true
        validate: "value >= params.start_time and value <= utcnow()"
      event_types:
        type: list[string]
        required: false
        default: []
        max_items: 50
      limit:
        type: integer
        required: false
        default: 1000
        min: 1
        max: 10000

    # Query template (Jinja2 + ClickHouse typed params)
    template: |
      SELECT
        toDate(timestamp) AS day,
        event_type,
        count(*) AS events
      FROM {org_id:Identifier}.events
      WHERE timestamp BETWEEN {start_time:DateTime64} AND {end_time:DateTime64}
      {% if event_types %}
        AND event_type IN ({event_types:Array(String)})
      {% endif %}
      GROUP BY day, event_type
      ORDER BY day DESC
      LIMIT {limit:UInt32}

    # Safety guards
    guards:
      timeout_seconds: 30
      max_rows: 10000
      explain_threshold_ms: 5000

    # Caching (optional)
    cache:
      enabled: true
      ttl_seconds: 300
      vary_by:
        - org_id
        - start_time
        - end_time
        - event_types
```

### Prometheus Query Example

```yaml
# queries/prometheus/metrics.yaml
queries:
  cpu_by_host:
    store: prometheus
    description: CPU usage per host

    roles:
      - admin
      - analyst
      - sre

    params:
      org_id:
        type: string
        scope: tenant
      window:
        type: duration
        default: "5m"
        max: "1h"
      host:
        type: string
        required: false

    # org_id injected as mandatory label matcher
    template: |
      avg(rate(cpu_usage_seconds_total{org="{{org_id}}"{% if host %}, host="{{host}}"{% endif %}}[{{window}}])) by (host)

    guards:
      timeout_seconds: 30
      max_time_range: "7d"

    cache:
      enabled: true
      ttl_seconds: 60
```

### PostgreSQL Query Example

```yaml
# queries/postgres/users.yaml
queries:
  users_by_org:
    store: postgres
    description: List users in organisation

    roles:
      - admin

    params:
      org_id:
        type: uuid
        scope: tenant
      status:
        type: string
        enum: [active, inactive, pending]
        required: false
      limit:
        type: integer
        default: 100
        max: 1000

    # psycopg3 parameterised query (%(name)s syntax)
    template: |
      SELECT id, email, status, created_at
      FROM users
      WHERE org_id = %(org_id)s
      {% if status %}AND status = %(status)s{% endif %}
      ORDER BY created_at DESC
      LIMIT %(limit)s

    guards:
      timeout_seconds: 10

    # No caching for PostgreSQL (as per requirements)
    cache:
      enabled: false
```

### Kafka Query Examples

Kafka queries use hs-pylib's `ReadOnlyKafkaClient` for safe, read-only operations.

```yaml
# queries/kafka/topics.yaml
queries:
  list_topics:
    store: kafka
    description: List all Kafka topics with partition counts

    roles:
      - admin
      - analyst
      - sre

    params:
      org_id:
        type: string
        scope: tenant
      include_internal:
        type: boolean
        default: false

    # No template - uses ReadOnlyKafkaClient.list_topics()
    operation: list_topics

    guards:
      timeout_seconds: 10

    cache:
      enabled: true
      ttl_seconds: 60

  topic_metadata:
    store: kafka
    description: Get detailed topic metadata including watermarks

    roles:
      - admin
      - sre

    params:
      org_id:
        type: string
        scope: tenant
      topic:
        type: string
        required: true
        # Topic names are validated against org prefix
        validate: "value.startswith(params.org_id + '.')"

    operation: describe_topic

    guards:
      timeout_seconds: 15

    cache:
      enabled: true
      ttl_seconds: 30

  consumer_lag:
    store: kafka
    description: Get consumer group lag per partition

    roles:
      - admin
      - sre

    params:
      org_id:
        type: string
        scope: tenant
      group_id:
        type: string
        required: true
      topic:
        type: string
        required: true

    operation: get_consumer_lag

    guards:
      timeout_seconds: 30

    cache:
      enabled: true
      ttl_seconds: 15

  sample_messages:
    store: kafka
    description: Sample messages from a topic (reservoir sampling)

    roles:
      - admin
      - analyst

    params:
      org_id:
        type: string
        scope: tenant
      topic:
        type: string
        required: true
      count:
        type: integer
        default: 10
        min: 1
        max: 100
      start_time:
        type: datetime
        required: false
      end_time:
        type: datetime
        required: false

    operation: sample_messages

    guards:
      timeout_seconds: 60
      max_messages: 100

    # Sampling results are not cached (different each time)
    cache:
      enabled: false
```

**Kafka Executor Implementation** (uses hs-pylib):

```python
# src/dfe_engine/query_gateway/executors/kafka.py
from hs_pylib.kafka import ReadOnlyKafkaClient
from hs_pylib.kafka.sampling import reservoir_sample, time_bounded_consume

class KafkaExecutor:
    """Read-only Kafka executor using hs-pylib."""

    def __init__(self, config: dict):
        self.client = ReadOnlyKafkaClient(config)

    async def execute(
        self,
        operation: str,
        params: dict,
        guards: QueryGuards,
    ) -> QueryResult:

        org_id = params["org_id"]

        if operation == "list_topics":
            topics = self.client.list_topics(
                include_internal=params.get("include_internal", False)
            )
            # Filter to org's topics only
            org_topics = [t for t in topics if t.name.startswith(f"{org_id}.")]
            return QueryResult(data=[t._asdict() for t in org_topics])

        elif operation == "describe_topic":
            topic = params["topic"]
            # Validate topic belongs to org
            if not topic.startswith(f"{org_id}."):
                raise PermissionDeniedError(f"Topic {topic} not in org {org_id}")
            metadata = self.client.describe_topic(topic)
            return QueryResult(data=metadata._asdict())

        elif operation == "get_consumer_lag":
            lag = self.client.get_consumer_lag(
                group_id=params["group_id"],
                topic=params["topic"],
            )
            return QueryResult(data=lag)

        elif operation == "sample_messages":
            # Uses hs-pylib sampling utilities
            messages = self._sample_messages(params, guards)
            return QueryResult(data=messages)

        raise ValueError(f"Unknown Kafka operation: {operation}")
```

---

## Parameterisation

### ClickHouse Native Parameters

ClickHouse supports typed query parameters that prevent SQL injection:

```sql
-- Template with typed placeholders
SELECT * FROM {org_id:Identifier}.events
WHERE timestamp BETWEEN {start:DateTime64} AND {end:DateTime64}
  AND user_id = {user_id:String}
LIMIT {limit:UInt32}
```

**Supported types:**
- `String`, `UInt32`, `UInt64`, `Int32`, `Int64`
- `DateTime`, `DateTime64`, `Date`
- `Array(T)`, `Identifier` (for table/column names)

### Jinja2 for Conditional Logic

Jinja2 handles optional parameters and conditional SQL blocks:

```sql
SELECT *
FROM {org_id:Identifier}.events
WHERE timestamp >= {start:DateTime64}
{% if event_types %}
  AND event_type IN ({event_types:Array(String)})
{% endif %}
{% if severity %}
  AND severity >= {severity:UInt8}
{% endif %}
```

### Rendering Pipeline

```python
class QueryRenderer:
    def render(self, query_def: QueryDefinition, params: dict) -> str:
        # 1. Jinja2 first pass (conditionals)
        template = Environment(autoescape=False).from_string(query_def.template)
        sql = template.render(**params)

        # 2. ClickHouse parameter substitution (type-safe)
        # The {param:Type} syntax is passed to ClickHouse client
        return sql
```

---

## EXPLAIN Integration

### Pre-Execution Cost Estimation

For ClickHouse queries, run EXPLAIN before execution to estimate cost:

```python
async def execute_with_explain(
    self,
    query: str,
    params: dict,
    guards: QueryGuards,
) -> tuple[Any, ExplainResult]:

    # Run EXPLAIN
    explain_query = f"EXPLAIN PLAN header=1 {query}"
    explain_result = await self.client.query(explain_query, parameters=params)

    estimated_rows = explain_result.get("estimated_rows", 0)
    estimated_ms = explain_result.get("elapsed_ms", 0)

    # Check against threshold
    if estimated_ms > guards.explain_threshold_ms:
        logger.warning(
            "Expensive query detected",
            estimated_ms=estimated_ms,
            threshold=guards.explain_threshold_ms,
        )

    # Execute actual query
    result = await self.client.query(query, parameters=params)

    return result, ExplainResult(
        estimated_rows=estimated_rows,
        estimated_ms=estimated_ms,
        plan=explain_result.get("plan"),
    )
```

### API Response with EXPLAIN

```json
{
  "data": [...],
  "meta": {
    "query_id": "analytics.events_by_day",
    "rows": 1523,
    "duration_ms": 245,
    "cached": false,
    "explain": {
      "estimated_rows": 1600,
      "estimated_ms": 200,
      "read_bytes": 1048576
    }
  }
}
```

---

## Caching Layer

### PostgreSQL-Backed Cache

Shared cache accessible by all pod instances, storing results for ClickHouse and Prometheus queries.

**Schema:**

```sql
CREATE TABLE query_cache (
    cache_key TEXT PRIMARY KEY,
    query_id TEXT NOT NULL,
    org_id TEXT NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    hit_count INTEGER DEFAULT 0,
    size_bytes INTEGER
);

CREATE INDEX idx_cache_expiry ON query_cache(expires_at);
CREATE INDEX idx_cache_org ON query_cache(org_id);
CREATE INDEX idx_cache_query ON query_cache(query_id);
```

### Cache Key Generation

```python
def cache_key(query_id: str, params: dict, org_id: str) -> str:
    """Generate deterministic cache key from query and parameters."""
    # Only include vary_by params in hash
    vary_params = {k: v for k, v in params.items() if k in query_def.cache.vary_by}
    params_hash = hashlib.sha256(
        json.dumps(vary_params, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return f"qc:{org_id}:{query_id}:{params_hash}"
```

### Integration with hs-pylib Cache

Extend hs-pylib's cache module with a PostgreSQL backend:

```python
# src/dfe_engine/query_gateway/cache.py
from typing import Any
from psycopg_pool import AsyncConnectionPool
from hs_pylib.logger import logger

class PostgresCacheBackend:
    """PostgreSQL-backed cache for multi-instance query result sharing."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def get(self, key: str) -> Any | None:
        async with self.pool.connection() as conn:
            row = await conn.execute(
                """
                UPDATE query_cache
                SET hit_count = hit_count + 1
                WHERE cache_key = $1 AND expires_at > NOW()
                RETURNING data
                """,
                (key,)
            ).fetchone()
            return row["data"] if row else None

    async def set(
        self,
        key: str,
        data: Any,
        ttl_seconds: int,
        query_id: str,
        org_id: str,
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO query_cache (cache_key, query_id, org_id, data, expires_at, size_bytes)
                VALUES ($1, $2, $3, $4, NOW() + $5 * INTERVAL '1 second', $6)
                ON CONFLICT (cache_key) DO UPDATE SET
                    data = EXCLUDED.data,
                    expires_at = EXCLUDED.expires_at,
                    hit_count = 0,
                    size_bytes = EXCLUDED.size_bytes
                """,
                (key, query_id, org_id, Json(data), ttl_seconds, len(str(data)))
            )

    async def invalidate_org(self, org_id: str) -> int:
        """Invalidate all cache for an organisation (e.g., after data load)."""
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "DELETE FROM query_cache WHERE org_id = $1",
                (org_id,)
            )
            return result.rowcount

    async def invalidate_query(self, query_id: str, org_id: str | None = None) -> int:
        """Invalidate cache for a specific query, optionally scoped to org."""
        async with self.pool.connection() as conn:
            if org_id:
                result = await conn.execute(
                    "DELETE FROM query_cache WHERE query_id = $1 AND org_id = $2",
                    (query_id, org_id)
                )
            else:
                result = await conn.execute(
                    "DELETE FROM query_cache WHERE query_id = $1",
                    (query_id,)
                )
            return result.rowcount

    async def cleanup_expired(self) -> int:
        """Periodic cleanup of expired entries. Run via scheduler."""
        async with self.pool.connection() as conn:
            result = await conn.execute(
                "DELETE FROM query_cache WHERE expires_at < NOW()"
            )
            return result.rowcount
```

---

## API Contract

### Execute Query

```
POST /api/query
Content-Type: application/json
Authorization: Bearer <jwt>

{
  "query_id": "analytics.events_by_day",
  "params": {
    "start_time": "2025-01-01T00:00:00Z",
    "end_time": "2025-01-15T00:00:00Z",
    "event_types": ["login", "logout"],
    "limit": 500
  },
  "options": {
    "explain": false,
    "explain_only": false,
    "bypass_cache": false
  }
}
```

**Response:**

```json
{
  "status": "ok",
  "data": [
    {"day": "2025-01-15", "event_type": "login", "events": 1234},
    {"day": "2025-01-15", "event_type": "logout", "events": 1100}
  ],
  "meta": {
    "query_id": "analytics.events_by_day",
    "store": "clickhouse",
    "rows": 30,
    "duration_ms": 45,
    "cached": true,
    "cache_age_seconds": 120,
    "org_id": "acme-corp"
  }
}
```

**With EXPLAIN:**

```json
{
  "status": "ok",
  "data": [...],
  "meta": {
    "query_id": "analytics.events_by_day",
    "rows": 30,
    "duration_ms": 45,
    "explain": {
      "estimated_rows": 35,
      "estimated_ms": 40,
      "read_bytes": 524288,
      "plan": "Expression\n  ReadFromMergeTree..."
    }
  }
}
```

### List Available Queries

```
GET /api/query/registry
Authorization: Bearer <jwt>

Response:
{
  "queries": [
    {
      "id": "analytics.events_by_day",
      "store": "clickhouse",
      "description": "Daily event counts",
      "params": {
        "start_time": {"type": "datetime", "required": true},
        "end_time": {"type": "datetime", "required": true},
        "event_types": {"type": "list[string]", "required": false}
      }
    }
  ]
}
```

### Invalidate Cache

```
POST /api/query/cache/invalidate
Authorization: Bearer <jwt>
X-Org-Id: acme-corp

{
  "query_id": "analytics.events_by_day",  // optional - if omitted, invalidates all for org
}

Response:
{
  "status": "ok",
  "invalidated": 15
}
```

---

## Resource Limits

| Store | Mechanism | Configuration |
|-------|-----------|---------------|
| ClickHouse | EXPLAIN threshold + query timeout + max_rows | Per-query in YAML |
| PostgreSQL | `statement_timeout` at connection level | Settings module |
| Prometheus | Time range limits + timeout | Per-query in YAML |

### ClickHouse Settings

```python
# Applied per-query via ClickHouse settings
query_settings = {
    "max_execution_time": guards.timeout_seconds,
    "max_result_rows": guards.max_rows,
    "max_result_bytes": guards.max_bytes or 100_000_000,  # 100MB default
}
```

### PostgreSQL Settings

```python
# Connection-level timeout
async with pool.connection() as conn:
    await conn.execute(f"SET statement_timeout = '{timeout_ms}'")
    result = await conn.execute(query, params)
```

---

## Audit Logging

All query executions are logged with full context:

```python
logger.info(
    "Query executed",
    query_id=query_id,
    org_id=org_id,
    principal=principal,
    store=query_def.store,
    duration_ms=duration_ms,
    rows=row_count,
    cached=was_cached,
    params_hash=params_hash,  # Not full params for security
    explain_ms=explain_result.estimated_ms if explain_result else None,
)
```

Audit fields:
- `timestamp`, `query_id`, `org_id`, `principal`
- `store`, `duration_ms`, `rows`, `cached`
- `status` (ok/error), `error_message` (if failed)
- `explain_estimated_ms`, `explain_read_bytes`

---

## Implementation Phases

### Phase 1: Core Infrastructure

1. Query registry loader (YAML → Python objects)
2. Parameter validator with type coercion
3. Query renderer (Jinja2 + typed params)
4. Base executor protocol

### Phase 2: RBAC Integration

1. Casbin model and adapter setup
2. Policy storage in PostgreSQL
3. Query-level role enforcement
4. Role management API

### Phase 3: Store Executors

1. ClickHouse executor with EXPLAIN
2. PostgreSQL executor with statement_timeout
3. Prometheus executor with label injection

### Phase 4: Caching

1. PostgresCache backend
2. Cache key generation
3. Cache invalidation API
4. Cleanup scheduler

### Phase 5: API Layer

1. `/api/query` endpoint in control-plane
2. `/api/query/registry` endpoint
3. `/api/query/cache/invalidate` endpoint
4. TypeScript SDK

---

## Security Properties

| Property | How Achieved |
|----------|--------------|
| No SQL injection | ClickHouse typed params, psycopg3 placeholders |
| Tenant isolation | Mandatory `org_id` in every query, RBAC domain enforcement |
| AuthZ | Casbin RBAC with per-query role definitions |
| Least privilege | Queries specify minimum required roles |
| Resource limits | EXPLAIN gates, timeouts, max_rows |
| Audit trail | Structured logging of all executions |
| No arbitrary queries | Query registry is the allowlist |
| Type safety | Parameter validation with strict schemas |

---

## References

- [Casbin Python (pycasbin)](https://github.com/casbin/pycasbin) - Authorization library
- [casbin-async-sqlalchemy-adapter](https://pypi.org/project/casbin-async-sqlalchemy-adapter/) - Async PostgreSQL adapter
- [Casbin RBAC Documentation](https://www.casbin.org/docs/rbac/) - Role-based access control
- [Casbin RBAC with Pattern](https://casbin.org/docs/rbac-with-pattern/) - Pattern matching for scalability
- [ClickHouse Query Parameters](https://clickhouse.com/docs/guides/developer/stored-procedures-and-prepared-statements) - Parameterised queries
- [ClickHouse Security Best Practices](https://www.wiz.io/blog/clickhouse-and-wiz) - Cloud database security
- [API Gateway RBAC Pattern](https://medium.com/@07rohit/designing-a-role-based-access-control-rbac-system-a-scalable-approach-441f05168933) - Scalable access control
