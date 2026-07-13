# Source - The Top-Level Data Abstraction

**Status:** Shipped (`SourceRegistry` + the Source model are the live path).

---

## Problem

Today, routing a data source through the DFE pipeline requires configuring
four independent systems that have no shared concept of "this is one source":

1. **Receiver** — field-match routing rules scattered in `category_to_topic`
2. **Transform** — separate vector/wasm source config with input/output topics
3. **Loader** — `category_to_table` routing with separate field lookups
4. **Schema** — per-table CSV definitions with no link back to the source

Adding a new data source (e.g. Filebeat, Syslog, CrowdStrike) means touching
all four systems independently and hoping the naming stays consistent. The API
and UI have no single entity to CRUD — they operate on disconnected config
fragments.

## Solution

Introduce **Source** as the top-level entity that the entire pipeline,
API, and UI revolve around.

A Source is a **data source** — a distinct stream of events entering the
platform (e.g. `filebeat`, `syslog`, `crowdstrike_edr`, `windows_audit`).
Everything flows from the source label.

---

## Two-Tier Architecture

The DFE platform splits cleanly into two tiers:

### Global Services (infrastructure — shared across all sources)

| Service | Role | Configured once |
|---------|------|-----------------|
| **receiver** | HTTP/gRPC ingestion, match routing, `_source` injection | Yes — one receiver config handles all sources |
| **loader** | Kafka → ClickHouse, schema-aware insert | Yes — one loader config handles all sources |
| **archiver** | Kafka → S3/file archive | Yes — one archiver config handles all sources |

These are infrastructure services. They scale horizontally but their
configuration is **not per-source** — a single receiver handles every source
via its match table.

### Source (data — one per data stream)

A Source **contains** all source-scoped components:

| Component | Required | Purpose |
|-----------|----------|---------|
| **identity** | Yes | `_source` label, display name, match rule |
| **schema** | Yes | ClickHouse table definition (starts as common header only). See [SCHEMA.md](schema.md) |
| **fetcher** | No | SaaS API pull (CrowdStrike, M365, Okta, etc.) |
| **transform** | No | Enrichment/normalisation stage (vector or wasm) |
| **rules** | No | SQL detection queries against this source's table |
| **sigma** | No | Sigma field mappings + auto-generated compatibility view |

Instead of configuring fetchers, transforms, schemas, and detection rules
as independent systems, they are **nested inside the Source they belong to**.
The API and UI revolve around Sources — not around services.

```
Global (configure once)         Source (one per data stream)
┌─────────────────────┐         ┌─────────────────────────────┐
│ receiver            │         │ source: filebeat            │
│ loader              │         │   ├── match rule            │
│ archiver            │         │   ├── schema (mandatory)    │
└─────────────────────┘         │   ├── transform (optional)  │
                                │   ├── fetcher (optional)    │
                                │   ├── rules (optional)      │
                                │   └── sigma (optional)      │
                                ├─────────────────────────────┤
                                │ source: crowdstrike_edr     │
                                │   ├── match rule            │
                                │   ├── schema (mandatory)    │
                                │   ├── transform (optional)  │
                                │   ├── fetcher (mandatory)   │
                                │   └── rules (optional)      │
                                ├─────────────────────────────┤
                                │ source: syslog              │
                                │   ├── match rule            │
                                │   ├── schema (mandatory)    │
                                │   └── rules (optional)      │
                                └─────────────────────────────┘
```

---

## Source Definition

```yaml
# Example: sources/filebeat.yaml
source: filebeat                        # The _source label — immutable identifier
display_name: Filebeat
description: Elastic Filebeat log collector
enabled: true

# --- Header ---
# Common schema header. When a source is first created, the schema starts
# as JUST this header — the common fields for the selected type + version.
# Source-specific fields are added incrementally.
header:
  type: time_series                     # Schema type (time_series, metric, alert, etc.)
  version: 1.0.0                        # Common header version (semver)

# --- Receiver Match ---
# How the receiver identifies this source from incoming data.
# The receiver evaluates match rules and sets _source in the JSON payload.
match:
  field: tags.collector.type            # JSON field to inspect
  value: filebeat                       # Expected value (exact match)
  # Future: regex, multi-field, compound rules

# --- Topics (derived, not configured) ---
# topic_land: filebeat_land             # Auto: {_source}_land
# topic_load: filebeat_load             # Auto: {_source}_load (only if transform exists)

# --- Fetcher (optional) ---
# Only for SaaS API sources that need active polling.
# Push-based sources (filebeat, syslog, etc.) do not have a fetcher.
# fetcher:
#   source_type: crowdstrike
#   base_url: https://api.crowdstrike.com
#   auth:
#     type: oauth2
#     token_url: https://api.crowdstrike.com/oauth2/token
#   poll_interval_secs: 300

# --- Transform (optional) ---
# If present, data flows: _land → transform → _load
# If absent, data flows: _land → loader directly
transform:
  engine: vector                        # vector | wasm
  config_file: /etc/vector/filebeat.yaml
  # engine-specific fields follow the existing VectorSourceConfig / WasmSourceConfig
  env: {}                               # Per-transform ENV overrides
  files: []                             # Enrichment files (CSV, MMDB)

# --- Schema (mandatory) ---
# The source owns its ClickHouse table schema.
# One source = one table = one schema.
# On creation, the schema is just the common header for the selected type+version.
# Source-specific fields are added over time.
schema:
  meta_schema: logs_beats_filebeat      # Base field definitions (YAML)
  meta_schema_version: 1.0.0
  derived_schema: filebeat/derived      # Source-specific field overrides (optional)
  additional_fields: filebeat/add       # Extra fields, indexes (optional)
  ttl_days: 90                          # Data retention
  engine: MergeTree                     # MergeTree | ReplicatedMergeTree | SharedMergeTree
```

### Minimal Source (just match + common header)

A source can be created with almost nothing — the schema starts as just the
common header fields for the selected type:

```yaml
source: syslog
display_name: Syslog
match:
  field: tags.collector.type
  value: syslog
header:
  type: time_series
  version: 1.0.0
schema:
  ttl_days: 90
```

No fetcher, no transform. The receiver matches and routes to `syslog_land`,
the loader picks it up and inserts into `{db}.syslog` using the common
time_series header schema. Fields can be added incrementally later.

---

## The `_source` Label

The `_source` label is the **single identifier** that ties everything together.
It is a fixed, lowercase, underscore-separated string (e.g. `filebeat`,
`crowdstrike_edr`).

The receiver **sets `_source` in the JSON payload** at ingestion time. From
that point, everything is derived automatically:

```
_source = "filebeat"

Kafka topics:
  filebeat_land     — raw data from receiver
  filebeat_load     — transformed data (only if transform exists)

ClickHouse:
  Table: {db}.filebeat
  _source column value: "filebeat"
```

### Naming Rules

- Lowercase alphanumeric + underscores only: `[a-z][a-z0-9_]*`
- No hyphens (ClickHouse table names)
- Max 64 characters
- Must be unique across all sources

---

## Data Flow

### With Transform

```
Data Source (e.g. Filebeat agent)
  │
  ▼
Receiver
  │  Matches: tags.collector.type == "filebeat"
  │  Sets: _source = "filebeat"
  │  Produces to: filebeat_land
  │
  ▼
Kafka: filebeat_land
  │
  ▼
Transform (vector or wasm)
  │  Consumes: filebeat_land
  │  Enriches, normalises, maps fields
  │  Produces to: filebeat_load
  │
  ▼
Kafka: filebeat_load
  │
  ▼
Loader
  │  Consumes: filebeat_load
  │  Routes by _source → {db}.filebeat table
  │  Applies schema, coercion, metadata injection
  │
  ▼
ClickHouse: {db}.filebeat
```

### Without Transform (direct load)

```
Data Source
  │
  ▼
Receiver
  │  Sets: _source = "syslog"
  │  Produces to: syslog_land
  │
  ▼
Kafka: syslog_land
  │
  ▼
Loader
  │  Consumes: syslog_land
  │  Routes by _source → {db}.syslog table
  │
  ▼
ClickHouse: {db}.syslog
```

When no transform is configured for a source, the loader consumes directly
from `{_source}_land`. The `_load` topic is never created.

---

## How the Receiver Routes

Today the receiver uses `event_category` field lookups and a
`category_to_topic` mapping. The Source model changes this to:

1. Receiver loads all Source definitions at startup
2. For each incoming event, evaluates `match` rules in priority order
3. First match wins — sets `_source` in the JSON payload
4. Produces to `{_source}_land` topic
5. Unmatched events go to `unmatched_land` (unchanged)

The `_source` field becomes a **first-class field in every event**, injected
by the receiver before the data hits Kafka. Downstream services (transform,
loader) consume it directly — no independent routing config needed.

### Receiver Config Change

The receiver's `routing.category_to_topic` dict is replaced by the Source
registry. The receiver needs only:

```yaml
routing:
  source_field: _source           # Field name set in JSON (fixed)
  topic_suffix_land: _land        # Topic suffix for raw data
  topic_suffix_load: _load        # Topic suffix for transformed data
  default_source: unmatched       # Fallback when no match rule hits
```

Match rules come from the Source definitions, not from receiver config.

---

## How the Loader Routes

Today the loader uses `table_fields` + `category_to_table` to determine the
target table. With Source:

1. Loader reads `_source` from the JSON payload (set by receiver)
2. `_source` directly maps to the target table: `{db}.{_source}`
3. No `category_to_table` mapping needed — the source label IS the table name
4. Schema for that table is defined in the Source definition

```yaml
# Loader config simplifies to:
routing:
  source_field: _source           # Field containing the source label
  default_db: common              # Database (or per-org with org_id)
  # No category_to_table — _source IS the table name
```

---

## Schema Ownership

Each Source owns exactly one ClickHouse table schema. The relationship is 1:1:

```
Source: filebeat
  └── Schema: logs_beats_filebeat
        ├── meta_schema.yaml       (base columns, types, use cases)
        ├── derived_schema.yaml    (source-specific overrides)
        └── additional_fields.yaml (enrichment fields)
```

The global **type registry** (`type_registry.yaml`) maps primitives to
ClickHouse types — it's shared across all sources, not per-source.

The schema section in the Source definition feeds directly into the existing
`SchemaBuilder` / `ClickHouseSchema` pipeline. The only change is that schema
lifecycle (create, migrate, drop) is triggered from the Source, not from a
standalone schema config.

### Schema Metadata

Schema definitions are YAML. See [SCHEMA.md](schema.md) for the full
type system, primitive-to-ClickHouse mapping, and use case definitions.

```yaml
# meta_schema.yaml example (excerpt)
columns:
  - name: user_name
    type: string
    use_case: dimension
    comment: "@source: first(user_id/uid/id)"
  - name: source_ip
    type: ip
    use_case: range
    comment: "@source: src_ip"
  - name: severity
    type: string
    attribute: lowcardinality
    use_case: dimension
  - name: message
    type: text
    use_case: fulltext
```

Loader directives in the `comment` field are preserved through to
ClickHouse column comments in DDL.

### Schema Lifecycle

| Source Event | Schema Action |
|-------------|---------------|
| Source created | `CREATE TABLE IF NOT EXISTS` |
| Schema YAML updated | `ALTER TABLE` via SchemaModifier (add/modify columns) |
| Source disabled | No action (table remains, no new data) |
| Source deleted | Table retained (data preservation) — manual DROP if needed |

---

## Source Registry

Sources are managed as YAML files in the config directory, following the
same pattern as service configs:

```
<config_directory>/
  sources/
    filebeat.yaml
    syslog.yaml
    crowdstrike_edr.yaml
    windows_audit.yaml
    ...
```

Backed by `DirectoryConfigStore` — same git-aware, cached, callback-driven
store used by `ServiceConfigRegistry`.

### CRUD Operations

| Operation | Effect |
|-----------|--------|
| **Create** | Validates source definition, creates Kafka topics (or marks for creation), runs schema DDL, updates receiver match rules |
| **Read** | Returns source config + status (topic exists, table exists, transform running) |
| **Update** | Validates changes, applies schema migration if fields changed, updates receiver/transform config |
| **Delete** | Soft-delete (disable) by default. Hard-delete removes match rule + transform config but preserves table + data |
| **List** | All sources with status overlay (healthy, degraded, disabled) |

### Validation Rules

- `_source` label must be unique
- `_source` label must match naming rules (`[a-z][a-z0-9_]*`, max 64 chars)
- Match rules must not conflict (two sources matching the same field+value)
- If transform is specified, engine must be `vector` or `wasm`
- Schema YAML files must exist and pass SchemaBuilder validation
- Schema must include `_source` as a column (injected if missing)

---

## What Changes

### Global services (receiver, loader, archiver)

These services get **simpler** — their per-source routing config is replaced
by the Source registry:

| Service | Before (per-service config) | After (source-driven) |
|---------|---------------------------|----------------------|
| **Receiver** | `routing.category_to_topic` dict | Compiled match table from Source definitions |
| **Loader** | `routing.category_to_table` dict | `_source` field → `{db}.{_source}` table (direct) |
| **Archiver** | No change | No change (archives all topics) |

Global service configs still own infrastructure concerns: Kafka brokers,
ClickHouse connection, TLS, SASL, buffer sizes, KEDA scaling. Source-specific
routing moves out.

### Source-scoped components (fetcher, transform, schema)

These move **into the Source definition** — no longer configured as
standalone service-level sources:

| Before | After |
|--------|-------|
| `transform-vector` service config with `sources: [...]` list | Each Source's `transform:` section (if needed) |
| `transform-wasm` service config with `sources: [...]` list | Each Source's `transform:` section (if needed) |
| `fetcher` service config with `sources: [...]` list | Each Source's `fetcher:` section (if needed) |
| Standalone schema definitions in `dfe_package.yaml` | Each Source's `schema:` section (mandatory, YAML) |

The service-level config for transform/fetcher reduces to just infrastructure:
Kafka brokers, resource limits, IPC settings. The per-source config (what to
transform, what to fetch) lives in the Source.

### dfe-engine (this repo)

- New `Source` Pydantic model + `SourceRegistry` (CRUD, validation, git-backed)
- Schema lifecycle tied to Source (create/migrate on source change)
- Config generators: Source definitions → receiver match table, loader routing,
  transform source entries, fetcher source entries
- Sources become the primary entity for the API and UI

### API and CLI surface (shipped in this repo)

The control-plane surface landed IN the engine (the separate
dfe-control-plane repo was retired before it existed):

- REST API and CLI expose Sources as the primary entity
- Source CRUD endpoints replace per-service config editing for routing
- UI navigates by Source - not by service

---

## Relationship to Services

### Two tiers, not two systems

**Global services** (receiver, loader, archiver) are infrastructure —
configured once, shared across all sources. Their configs define *how*
(scaling, resources, connection strings, Kafka brokers).

**Sources** define *what* — each data stream and its scoped components
(fetcher, transform, schema). A source's fetcher and transform are
**contained within the source**, not managed as standalone service instances.

```
                    Global (shared infra)
                    ┌──────────────────────┐
                    │ receiver-1           │◄── all match rules from sources
                    │ loader-1             │◄── all _source → table mappings
                    │ archiver-1           │◄── all topics
                    └──────────────────────┘

Source: filebeat                Source: crowdstrike_edr        Source: syslog
┌────────────────────┐          ┌────────────────────┐        ┌─────────────────┐
│ match: tags...=fb  │          │ match: tags...=cs  │        │ match: tags...  │
│ schema: time_series│          │ fetcher: oauth2    │        │ schema: time_s  │
│ transform: vector  │          │ schema: time_series│        │ (no transform)  │
└────────────────────┘          │ transform: vector  │        │ (no fetcher)    │
  filebeat_land ──▶ transform   └────────────────────┘        └─────────────────┘
  filebeat_load ──▶ loader        cs_edr_land ──▶ transform     syslog_land ──▶ loader
                                  cs_edr_load ──▶ loader
```

### How source components map to service instances

Source-scoped fetchers and transforms are **logical config** — they describe
what needs to happen. The actual execution happens on shared service instances:

| Source component | Runs on | How |
|-----------------|---------|-----|
| `source.match` | Global receiver | Compiled into receiver match table |
| `source.fetcher` | Shared fetcher instance(s) | Fetcher loads source configs, polls each |
| `source.transform` | Shared transform instance(s) | Transform loads source configs, processes each |
| `source.schema` | dfe-engine | Schema DDL generated and applied by engine |
| `source.rules` | Hunt scheduler | SQL queries executed on cron, matches → alerts |
| `source.sigma` | dfe-engine | Generates compatibility view for Sigma field names |

A single transform-vector deployment may process transforms for 20 different
sources. The source definition says *this source needs a vector transform
with this config* — the deployment layer decides *which transform instance
runs it*.

---

## Rules

A **Rule** is a SQL detection query tied to a Source. It runs against the
source's ClickHouse table and produces matches (detections).

```yaml
# rules/brute_force_login.yaml
rule: brute_force_login
source: windows_audit                   # Tied to this source's table
display_name: Brute Force Login Attempt
severity: high

query: |
  SELECT
    _timestamp,
    _org_id,
    user_name,
    source_ip,
    count() AS attempt_count
  FROM {db}.{source}
  WHERE event_id = 4625
    AND _timestamp >= {from}
    AND _timestamp < {to}
  GROUP BY _timestamp, _org_id, user_name, source_ip
  HAVING attempt_count >= {threshold}

parameters:
  threshold: 5

schedule: "*/5 * * * *"                 # Cron schedule for hunt execution
```

Rules reference `{db}`, `{source}`, `{from}`, `{to}` — templated at
execution time. The rule's SQL runs against the source's table, so it
has access to exactly the columns defined in the source's schema.

---

## Hunts

A **Hunt** is a scheduled batch execution of Rules. Hunt results
(matches/detections) are written to the **alerts table**.

```
Rule (SQL query tied to a Source)
  │
  ▼
Hunt Scheduler (cron)
  │  Expands {from}/{to} time window
  │  Runs rule SQL against {db}.{source}
  │
  ▼
Matches (detection results)
  │
  ▼
Alerts Table ({db}.alerts)
  │  Common header (timeseries profile)
  │  + rule_name, severity, source, match_data
```

The alerts table is itself a Source (`_source = "alerts"`) with
`header.type: timeseries`. This gives it the same schema management,
retention, and query capabilities as any other source — including the
ability to write rules against alerts (meta-detection / correlation).

---

## Sigma

Sigma rules reference fields by standardised names (`SourceIP`,
`CommandLine`, `EventID`). Rather than embedding Sigma names in the base
schema, each source can have a **Sigma compatibility view**:

```sql
-- Auto-generated from source schema + sigma field mapping
CREATE VIEW {db}.{source}_sigma AS
SELECT
    source_ip AS SourceIP,
    dest_ip AS DestinationIP,
    user_name AS User,
    command_line AS CommandLine,
    event_id AS EventID,
    *
FROM {db}.{source}
```

Sigma rules query the view (standard field names). The base schema stays
ClickHouse-native. The mapping is per-source — different sources map
differently. Zero storage overhead.

```yaml
# In source definition
sigma:
  taxonomy: windows                     # Built-in mapping set
  custom_mappings:                      # Per-source overrides
    CommandLine: command_line
    ParentCommandLine: parent_cmd
```

Converted Sigma rules become DFE Rules tied to the source, querying the
`{source}_sigma` view. See [SCHEMA.md](schema.md) for details.

---

## Elastic Index Template Converter

The existing Elastic/OpenSearch index template converter generates a source
schema from a supplied Elastic template JSON. This is the primary onboarding
path for migrating from Elastic:

```
Elastic Template JSON → Converter → Source schema YAML → Source definition
```

The converter maps Elastic types to primitives, Elastic analyzers to
use_cases, and preserves the field hierarchy as flat columns. See
[SCHEMA.md](schema.md) for the type mapping table.

This is tied to a Source — when creating a source from an Elastic data
stream, the converter bootstraps the schema so the user doesn't start from
scratch.

---

## API / UI Model

With Source as the top-level entity, the API simplifies to two sections:

### Global (infrastructure)

```
GET    /api/v1/global/receiver          # Receiver config
PUT    /api/v1/global/receiver          # Update receiver config
GET    /api/v1/global/loader            # Loader config
PUT    /api/v1/global/loader            # Update loader config
GET    /api/v1/global/archiver          # Archiver config
PUT    /api/v1/global/archiver          # Update archiver config
```

Rarely touched after initial setup. Infrastructure concern.

### Sources (data)

```
GET    /api/v1/sources                  # List all sources
POST   /api/v1/sources                  # Create source (match + header → schema auto-created)
GET    /api/v1/sources/{source}         # Get source (includes status)
PUT    /api/v1/sources/{source}         # Update source
DELETE /api/v1/sources/{source}         # Disable/delete source

# Source sub-resources
GET    /api/v1/sources/{source}/schema  # Get schema definition
PUT    /api/v1/sources/{source}/schema  # Update schema (triggers ALTER TABLE)
GET    /api/v1/sources/{source}/transform  # Get transform config
PUT    /api/v1/sources/{source}/transform  # Set/update transform
DELETE /api/v1/sources/{source}/transform  # Remove transform
GET    /api/v1/sources/{source}/fetcher    # Get fetcher config
PUT    /api/v1/sources/{source}/fetcher    # Set/update fetcher
DELETE /api/v1/sources/{source}/fetcher    # Remove fetcher
GET    /api/v1/sources/{source}/rules      # List rules for this source
POST   /api/v1/sources/{source}/rules      # Create rule
GET    /api/v1/sources/{source}/rules/{rule}  # Get rule
PUT    /api/v1/sources/{source}/rules/{rule}  # Update rule
DELETE /api/v1/sources/{source}/rules/{rule}  # Delete rule
GET    /api/v1/sources/{source}/sigma      # Get sigma mapping + view status
PUT    /api/v1/sources/{source}/sigma      # Set/update sigma mapping
GET    /api/v1/sources/{source}/status     # Health: topics, table, transform, fetcher
```

The day-to-day workflow revolves around Sources. Adding a new data source is
one POST. The engine handles topic creation, schema DDL, and wiring.

---

## Migration Path

The Source model is **additive** — existing service configs continue to work.
Migration is incremental:

1. **Phase 1:** Source model + registry in dfe-engine (Python). Sources as
   YAML SSoT. Schema lifecycle wired to source. Config generators produce
   receiver/loader/transform/fetcher configs from Source definitions.
   Services still read their own config format — no Rust changes.
2. **Phase 2:** Control plane API exposes Source CRUD. UI built around
   Sources. Global service configs exposed as simple forms.
3. **Phase 3:** Rust services read Source definitions directly (or a compiled
   routing table). Service-level per-source routing config deprecated.

Phase 1 is implementable now without any Rust changes.
