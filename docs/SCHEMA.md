# Schema Type System v2

**Status:** Proposed
**Last Updated:** 2026-02-28

---

## Problem

The current schema type system conflates three separate concerns into a
single `type` field:

```csv
column,type,index_order,index_type,comment
user_name,string_fast,,dimension,
source_ip,ipv4,,,
message,text,,text_search,
category,string_lowcardinality,,dimension,
```

- `string_fast` = type (string) + codec hint (LZ4 for speed)
- `string_lowcardinality` = type (string) + attribute (LowCardinality)
- `string_fast_lowcardinality` = type + codec + attribute (all combined)
- `text` = type (string) + use case hint (large text)
- `text_search` in `index_type` = another use case layer

This leads to a combinatorial explosion of types. Adding a new attribute
(e.g. `Nullable` control) or a new codec preference means creating new
compound type names. The type system also has no way to:

- Express loader field mapping directives (`@source`, `@renamed`, etc.)
- Specify exact ClickHouse types when the primitive isn't enough
- Attach use-case metadata separately from storage decisions

## Design Principles

### No Schema Registry

There is **no schema registry** — no CRUD service, no sync state, no
intermediate store. Two sources of truth exist, and that's it:

1. **Source YAML** — the meta schema definition (what the schema should be)
2. **ClickHouse DDL** — the deployed table (what the schema actually is)

The engine reads the Source YAML, generates DDL, and applies it to
ClickHouse. The loader reads the deployed table's columns and comments
from `system.columns` at runtime. There is nothing in between.

Schema changes flow in one direction:

```
Source YAML (intent) → engine generates DDL → ClickHouse (deployed truth)
                                                    ↑
                                        Rust K8s services read at runtime
                                        (loader, archiver — ONLY SSoT)
```

The Rust K8s services at scale **slave from the deployed ClickHouse
schema ONLY**. They read column types, comments, and table metadata
from `system.columns` at runtime — never from YAML, config files, or
any intermediate store. This means there is **never an in-flight
mismatch**: the engine deploys DDL, ClickHouse stores it, Rust services
read it. No sync, no cache invalidation race, no drift.

### Schema Lifecycle

| Event | Action |
|-------|--------|
| Source created | Engine generates `CREATE TABLE` from common header + source schema |
| Schema field added | Engine generates `ALTER TABLE ADD COLUMN` |
| Schema field modified | Engine generates `ALTER TABLE MODIFY COLUMN` (type/codec/comment) |
| Source disabled | No DDL change — table stays, no new data |
| Source deleted | Table preserved — manual `DROP TABLE` if needed |

The deployed ClickHouse table is always authoritative. If someone modifies
the table directly (via SQL), the engine detects drift on next schema
operation and can reconcile.

---

## Solution

Separate the schema column definition into **four independent axes**:

```
type + attribute + use_case + comment (includes loader directives)
```

Each axis is optional where it has sensible defaults.

---

## New Column Definition Format

```csv
column,type,attribute,use_case,default,index_order,comment
```

| Field | Required | Description |
|-------|----------|-------------|
| `column` | Yes | Column name |
| `type` | Yes | Primitive type OR exact ClickHouse type |
| `attribute` | No | Storage attribute (e.g. `lowcardinality`, `nullable`) |
| `use_case` | No | Query/index hint (e.g. `dimension`, `text_search`, `fulltext`) |
| `default` | No | DEFAULT expression |
| `index_order` | No | Position in ORDER BY / PRIMARY KEY |
| `comment` | No | Human description + loader directives |

### Example

```csv
column,type,attribute,use_case,default,index_order,comment
_timestamp_load,DateTime64(3),,,now64(3),0,@generated: now64(3)
_timestamp,DateTime64(3),,range,,1,@source: timestamp | now()
_uuid,UUID,,,generateUUIDv7(),,@generated: generateUUIDv7()
_org_id,string,lowcardinality,dimension,,,@source: org_id
_source,string,lowcardinality,dimension,,,@source: first(_source) | topic_name
_raw,string,nullable,text_search,,,@captured: raw_payload
_json,json,nullable,,,,@captured: raw_payload as JSON
_tags,json,nullable,,,,@source: first(tags/_tags/meta/metadata.tags)
user_name,string,,dimension,,,@source: first(user_id/uid/id)
source_ip,ip,,range,,,@source: src_ip
severity,string,lowcardinality,dimension,,,
message,string,,fulltext,,,
event_id,int32,,,,,"@source: event_id"
latency_ms,Float64,,range,,,
```

---

## Type Axis: Primitives and Exact Types

### Primitives

The type field accepts **primitives** — abstract types that map to ClickHouse
types with sensible defaults:

| Primitive | ClickHouse Type | Codec | Notes |
|-----------|----------------|-------|-------|
| `string` | `String` | `ZSTD(1)` | General purpose |
| `text` | `String` | `ZSTD(3)` | Large text, higher compression |
| `json` | `JSON` | `ZSTD(3)` | Native ClickHouse JSON (25.3+) |
| `bool` | `Bool` | `LZ4` | |
| `int8` | `Int8` | `ZSTD(1)` | |
| `int16` | `Int16` | `ZSTD(1)` | |
| `int32` | `Int32` | `ZSTD(1)` | |
| `int64` | `Int64` | `ZSTD(1)` | |
| `uint8` | `UInt8` | `ZSTD(1)` | |
| `uint16` | `UInt16` | `ZSTD(1)` | |
| `uint32` | `UInt32` | `ZSTD(1)` | |
| `uint64` | `UInt64` | `ZSTD(1)` | |
| `float32` | `Float32` | `ZSTD(1)` | |
| `float64` | `Float64` | `ZSTD(1)` | |
| `datetime` | `DateTime64(3,'UTC')` | `Delta, ZSTD(1)` | Nullable by default |
| `timestamp` | `DateTime64(3,'UTC')` | `Delta, LZ4` | NOT NULL — for ORDER BY |
| `date` | `Date` | `Delta, ZSTD(1)` | |
| `ip` | `IPv6` | `LZ4` | Stores both v4 and v6 |
| `ipv4` | `IPv4` | `T64, LZ4` | Explicit v4 only |
| `ipv6` | `IPv6` | `LZ4` | Explicit v6 only |
| `uuid` | `UUID` | — | No codec (already compact) |
| `geo_point` | `Point` | `ZSTD(1)` | |
| `enum8` | `Enum8(...)` | `ZSTD(1)` | Values in default field |
| `enum16` | `Enum16(...)` | `ZSTD(1)` | Values in default field |

### Exact ClickHouse Types

When a primitive isn't sufficient, specify the **exact ClickHouse type**
directly. The engine detects this by checking if the type string contains
parentheses or is a known CH type not in the primitive list:

```csv
column,type,attribute,use_case,default,index_order,comment
precision_ts,DateTime64(6,'UTC'),,,,,"Microsecond precision timestamp"
nested_data,"Nested(key String, value String)",,,,,
bitmap,AggregateFunction(groupBitmap UInt32),,,,,
```

When an exact type is specified:
- No automatic Nullable wrapping
- No automatic codec selection
- The type string is used verbatim in DDL
- Attributes are still applied if specified

This replaces the need for compound types like `string_fast` — if you want
LZ4 on a string, either use the `string` primitive (which gets ZSTD by default
and the engine picks codec based on use_case) or specify the exact CH type.

---

## Attribute Axis

Attributes modify how the type is stored. Multiple attributes can be
comma-separated:

| Attribute | Effect | Applicable To |
|-----------|--------|---------------|
| `lowcardinality` | Wraps in `LowCardinality(...)` | string, most types |
| `nullable` | Wraps in `Nullable(...)` | any type (default for most primitives) |
| `not_null` | Removes Nullable wrapper | overrides default |
| `materialized` | `MATERIALIZED` column | any type |
| `alias` | `ALIAS` column | any type |

### Nullability Defaults

- **Primitives** default to `Nullable` (except `timestamp`, `bool`)
- **Exact types** have no automatic wrapping
- Override with `not_null` or `nullable` attribute

### Examples

```csv
# LowCardinality string, not nullable
category,string,"lowcardinality,not_null",dimension,,,

# Nullable datetime (default)
last_seen,datetime,,,,,"@source: last_seen_at"

# Materialized column (computed from other columns)
day,Date,materialized,,toDate(_timestamp),,
```

---

## Use Case Axis

Use cases determine **indexing and query optimisation** — separated from
the type and attribute:

| Use Case | Index Generated | Granularity | When to Use |
|----------|----------------|-------------|-------------|
| `dimension` | `set(0)` | 4 | Exact match on low-medium cardinality values (status, category) |
| `fulltext` | `tokenbf_v1(8192, 4, 0)` | 4 | Token-based search on log messages |
| `text_search` | `ngrambf_v1(3, 256, 2, 0)` | 64 | Substring search on syslog/Windows messages |
| `bloom` | `bloom_filter` | 4 | General probabilistic filtering |
| `range` | `minmax` | 4 | Range queries on numeric/datetime values |
| `full_text_ga` | `full_text(0)` | 1 | ClickHouse 25.1+ native full-text (replaces fulltext) |
| _(empty)_ | No index | — | No query optimisation needed |

Use case is **independent of type** — you can add `dimension` to a string,
an int, or a boolean. The engine generates the appropriate index based on
the combination.

---

## Comment Axis: Loader Directives

The `comment` field serves double duty:

1. **Human documentation** — plain text description
2. **Loader directives** — `@` prefixed expressions that tell dfe-loader
   how to populate the field

### Directive Types (from dfe-loader DDL Expression Language)

| Directive | Purpose | Example |
|-----------|---------|---------|
| `@source: field` | Copy from source data | `@source: timestamp \| now()` |
| `@source: first(a/b/c)` | First non-null from list | `@source: first(user_id/uid/id)` |
| `@generated: expr` | ClickHouse DEFAULT — loader omits field | `@generated: now64(3)` |
| `@renamed: field` | Zero-copy field rename | `@renamed: logoriginal` |
| `@captured: payload` | Raw payload sidecar | `@captured: raw_payload as JSON` |
| `@computed: expr` | Derived/enriched value | `@computed: geoip(ip).country_code` |

### Precedence

When the loader resolves field mappings, column comments have **highest
precedence** — above built-in presets and external remap files. This means
the schema definition IS the authoritative field mapping.

### Comments in DDL

These directives are emitted as ClickHouse column comments in the generated
DDL:

```sql
`_timestamp` DateTime64(3,'UTC') CODEC(Delta, ZSTD(1))
    COMMENT '@source: timestamp | now()',
`_raw` Nullable(String) CODEC(ZSTD(3))
    COMMENT '@captured: raw_payload',
`user_name` Nullable(String) CODEC(ZSTD(1))
    COMMENT '@source: first(user_id/uid/id)',
```

The loader fetches column comments from `system.columns` and uses them to
build its per-table field mapping. No separate mapping config needed — the
schema IS the mapping.

---

## Common Header Profiles

The common header is not one-size-fits-all. Different data types need
different system fields. Profiles define which common header fields to
inject:

| Profile | Fields | Use Case |
|---------|--------|----------|
| **timeseries** (default) | `_timestamp_load`, `_timestamp`, `_timestamp_received`, `_uuid`, `_org_id`, `_source`, `_raw`, `_json`, `_tags` | Full event ingestion (logs, alerts, audit) |
| **minimal** | `_timestamp_load`, `_timestamp`, `_uuid`, `_org_id` | High-volume structured data (metrics, flow records) |
| **passthrough** | `_timestamp_load`, `_uuid`, `_org_id`, `_json` | Transparent bridge — no timestamp injection |

### Profile Tied to Source

Each Source selects a profile via the `header.type` field:

```yaml
# sources/filebeat.yaml
header:
  type: timeseries              # Profile name
  version: v001.000.000         # Common header version
```

When a source is created with `header.type: timeseries`, the schema starts
as just the timeseries common header fields. Source-specific data columns
are added incrementally.

### Profile Versioning

Tables are tagged with profile metadata in the table comment:

```sql
COMMENT '@schema_source: core | @schema_version: 2 | @profile: timeseries | @profile_version: 1'
```

Profile migrations (adding new common header fields) use `ALTER TABLE ADD
COLUMN` — safe, non-destructive, and automated.

---

## Sigma Column Mappings

Sigma rules reference fields by standardised names (e.g. `SourceIP`,
`CommandLine`, `EventID`). These must map to actual ClickHouse column names.

### Approach: Materialised View per Sigma Taxonomy

Rather than embedding Sigma field names in the base schema, create
**ClickHouse views** that expose Sigma-compatible column aliases:

```sql
-- Auto-generated from Source schema + Sigma field mapping
CREATE VIEW {db}.{source}_sigma AS
SELECT
    source_ip AS SourceIP,
    dest_ip AS DestinationIP,
    user_name AS User,
    process_name AS Image,
    command_line AS CommandLine,
    event_id AS EventID,
    *
FROM {db}.{source}
```

**Benefits:**
- Base schema stays clean (ClickHouse-native names)
- Sigma rules query the view (standard Sigma field names)
- Field mapping is per-source (different sources map differently)
- Zero storage overhead (view, not materialised table)
- Mappings generated from the existing `sigma/field_mapping_service.py`

### Sigma Mapping as Source Metadata

The Source definition can include a Sigma field mapping section:

```yaml
# sources/windows_audit.yaml
sigma:
  taxonomy: windows                     # Built-in mapping set
  custom_mappings:                      # Per-source overrides
    CommandLine: command_line
    ParentCommandLine: parent_cmd
```

The engine generates the Sigma view from source schema + mapping config.

---

## Elastic Index Template Converter

The existing Elastic/OpenSearch index template converter generates a
source meta schema from a supplied Elastic template JSON. This remains —
but now outputs into the Source schema format:

```
Elastic Template JSON
  │
  ▼
Template Converter (dfe-engine)
  │  Maps Elastic types → primitives
  │  Maps Elastic analyzers → use_cases
  │  Preserves field hierarchy as flat columns
  │
  ▼
Source schema CSV (new format)
  │  column, type, attribute, use_case, comment
  │
  ▼
Attached to a Source definition
```

### Elastic → Primitive Type Mapping

| Elastic Type | Primitive | Attribute | Use Case |
|-------------|-----------|-----------|----------|
| `keyword` | `string` | | `dimension` |
| `text` | `string` | | `fulltext` |
| `long` | `int64` | | |
| `integer` | `int32` | | |
| `short` | `int16` | | |
| `byte` | `int8` | | |
| `double` | `float64` | | |
| `float` | `float32` | | |
| `boolean` | `bool` | | |
| `date` | `datetime` | | |
| `ip` | `ip` | | `range` |
| `geo_point` | `geo_point` | | |
| `object` | `json` | | |
| `nested` | `json` | | |

The converter is invoked when creating a Source from an existing Elastic
data source — it bootstraps the schema so the user doesn't start from
scratch.

---

## Source-Scoped Entities

Beyond the schema itself, several other entities are scoped to a Source.
See [SOURCE.md](./SOURCE.md) for the full Source definition.

### Rules

A **Rule** is a SQL query tied to a Source. It runs against the source's
ClickHouse table and produces matches (detections).

```yaml
# rules/brute_force_login.yaml
rule: brute_force_login
source: windows_audit                   # Tied to this source
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

schedule: "*/5 * * * *"                 # Cron (for hunt scheduling)
```

Rules reference `{db}`, `{source}`, `{from}`, `{to}` — templated at
execution time. The rule's SQL runs against the source's table, so it
has access to exactly the columns defined in the source's schema.

### Hunts

A **Hunt** is a scheduled batch execution of one or more Rules. Hunt
results (matches/detections) are written to an **alerts table**.

```
Rule (SQL query)
  │  Tied to a Source
  │  Parameterised: {db}, {source}, {from}, {to}, {threshold}
  │
  ▼
Hunt (batch executor)
  │  Cron-scheduled
  │  Runs rule over time window
  │  Writes matches to alerts table
  │
  ▼
Alerts Table ({db}.alerts)
  │  Common header + alert-specific columns
  │  _source = original source
  │  rule_name, severity, match_data
```

The alerts table is itself a Source (with `header.type: timeseries`),
giving it the same schema management, retention, and query capabilities.

### Sigma Rules (converted)

Sigma rules are converted to DFE Rules via the existing `sigma_converter`.
The conversion produces a Rule definition with:

- SQL query generated from the Sigma detection logic
- Source determined by the Sigma `logsource` → Source mapping
- Sigma view used for field name compatibility

---

## Migration from v1 Type System

### Type Mapping (old → new)

| Old Type | New Type | Attribute | Use Case |
|----------|----------|-----------|----------|
| `string` | `string` | | |
| `string_fast` | `string` | | _(codec auto-selected)_ |
| `string_lowcardinality` | `string` | `lowcardinality` | |
| `string_fast_lowcardinality` | `string` | `lowcardinality` | |
| `text` | `text` | | |
| `json` | `json` | `nullable` | |
| `int8` | `int8` | | |
| `int16` | `int16` | | |
| `int32` | `int32` | | |
| `int64` | `int64` | | |
| `float32` | `float32` | | |
| `float64` | `float64` | | |
| `boolean` | `bool` | | |
| `timestamp` | `timestamp` | | |
| `datetime` | `datetime` | | |
| `ipv4` | `ipv4` | | |
| `ipv6` | `ipv6` | | |
| `ip_field` | `ip` | | |
| `geo_point` | `geo_point` | | |
| `uuid` | `uuid` | | |

### Index Type Mapping (unchanged)

| Old `index_type` | New `use_case` | Notes |
|-----------------|----------------|-------|
| `dimension` | `dimension` | Same |
| `fulltext` | `fulltext` | Same |
| `text_search` | `text_search` | Same |
| `hc` | `bloom` | Renamed for clarity |
| `range` | `range` | Same |
| `minmax` | `range` | Merged (both generated minmax) |

### CSV Format Change

```
# Old: column,type,default,index_order,index_type,comment
# New: column,type,attribute,use_case,default,index_order,comment
```

The migration is mechanical — a script maps old compound types to new
(type, attribute) pairs and renames `index_type` to `use_case`.
