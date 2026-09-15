# Schema Type System v2

**Status:** Shipped (`schema_builder_v2.py` is the live path). Since this
design landed, the schema YAML SSoT moved to the dfe-schemas repo (the
version-pinned `dfe-schemas` wheel), registries included, and the engine keeps no copies. Class-level lookup:
[schema-classes.md](schema-classes.md).

---

## Audience

This document is written for **data subject matter experts** -- people who
understand their data, their queries, and their retention requirements. You
do NOT need to be a ClickHouse expert. The schema system handles ClickHouse
internals (codecs, index tuning, Nullable wrapping, type widths) under the
hood. You pick meaningful types and describe how your data will be queried.
The engine does the rest.

If you need a specific ClickHouse type that the primitives don't cover,
there's an escape hatch (`ch_override`). But you shouldn't need it for 95%
of columns.

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
- `string_lowcardinality` = type (string) + storage attribute (LowCardinality)
- `string_fast_lowcardinality` = type + codec + attribute (all combined)

This leads to a combinatorial explosion. Adding a new attribute means
creating new compound type names. The format is CSV with no structure for
nested properties. The type system also has no way to:

- Express loader field mapping directives (`@source`, `@renamed`, etc.)
- Specify exact ClickHouse types when the primitive isn't enough
- Attach use-case metadata separately from storage decisions
- Validate that a use case makes sense for a given type

---

## Design Principles

### No Schema Registry

There is **no schema registry** -- no CRUD service, no sync state, no
intermediate store. Two sources of truth exist, and that's it:

1. **Source YAML** -- the meta schema definition (what the schema should be)
2. **ClickHouse DDL** -- the deployed table (what the schema actually is)

The engine reads the Source YAML, generates DDL, and applies it to
ClickHouse. The loader reads the deployed table's columns and comments
from `system.columns` at runtime. There is nothing in between.

Schema changes flow in one direction:

```
Source YAML (intent) → engine generates DDL → ClickHouse (deployed truth)
                                                    ↑
                                        Rust K8s services read at runtime
                                        (loader, archiver -- ONLY SSoT)
```

The Rust K8s services at scale **slave from the deployed ClickHouse
schema ONLY**. They read column types, comments, and table metadata
from `system.columns` at runtime -- never from YAML, config files, or
any intermediate store. This means there is **never an in-flight
mismatch**: the engine deploys DDL, ClickHouse stores it, Rust services
read it. No sync, no cache invalidation race, no drift.

### Schema Format: YAML

Schema definitions are **YAML**, not CSV. YAML supports structured data
(lists of attributes, nested overrides) and is consistent with the rest
of the Source definition format. The old CSV format is deprecated for
schema definitions.

**CSVs remain only for enrichment files** used by transform stages
(e.g. GeoIP lookups, threat intelligence feeds, asset inventories).
These are data files, not schema definitions.

### Schema Lifecycle

| Event | Action |
|-------|--------|
| Source created | Engine generates `CREATE TABLE` from common header + source schema |
| Schema field added | Engine generates `ALTER TABLE ADD COLUMN` |
| Schema field modified | Engine generates `ALTER TABLE MODIFY COLUMN` (type/codec/comment) |
| Source disabled | No DDL change -- table stays, no new data |
| Source deleted | Table preserved -- manual `DROP TABLE` if needed |

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

## Schema Definition Format (YAML)

```yaml
# meta_schema.yaml
columns:
  - name: _timestamp_load
    type: timestamp
    default: "now64(3)"
    order: 0
    comment: "@generated: now64(3)"

  - name: _timestamp
    type: datetime
    use_case: range
    order: 1
    comment: "@source: timestamp | now()"

  - name: _uuid
    type: uuid
    default: "generateUUIDv7()"
    comment: "@generated: generateUUIDv7()"

  - name: _org_id
    type: string
    attribute: [lowcardinality]
    use_case: dimension
    comment: "@source: org_id"

  - name: _source
    type: string
    attribute: [lowcardinality]
    use_case: dimension
    comment: "@source: first(_source) | topic_name"

  - name: _raw
    type: text
    use_case: text_search
    comment: "@captured: raw_payload"

  - name: _json
    type: json
    comment: "@captured: raw_payload as JSON"

  - name: _tags
    type: json
    comment: "@source: first(tags/_tags/meta/metadata.tags)"

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
    attribute: [lowcardinality]
    use_case: dimension

  - name: message
    type: text
    use_case: fulltext

  - name: event_id
    type: integer
    comment: "@source: event_id"

  - name: latency_ms
    type: float
    use_case: range
```

### Column Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Column name |
| `type` | Yes | Primitive type (see below) |
| `attribute` | No | List of storage attributes (e.g. `[lowcardinality]`) |
| `use_case` | No | Query pattern hint -- determines indexing (see below) |
| `default` | No | DEFAULT expression |
| `order` | No | Position in ORDER BY / PRIMARY KEY |
| `comment` | No | Human description + loader directives |
| `ch_override` | No | Exact ClickHouse type -- bypasses primitive mapping |

---

## Type Axis: Primitives

Primitives are **human-readable type names** that map to ClickHouse types
with sensible defaults. You don't need to know what `Int64` or `ZSTD(1)`
means -- pick the primitive that describes your data.

| Primitive | What It Is | Default ClickHouse Type |
|-----------|-----------|------------------------|
| `string` | Short-to-medium text (names, IDs, codes) | `String` |
| `text` | Large text (log messages, bodies, descriptions) | `String` |
| `integer` | Whole number (counts, IDs, event codes) | `Int64` |
| `float` | Decimal number (scores, latency, percentages) | `Float64` |
| `boolean` | True/false | `Bool` |
| `datetime` | Date and time with timezone | `DateTime64(3,'UTC')` |
| `timestamp` | Date and time -- never null (for ORDER BY / time columns) | `DateTime64(3,'UTC')` |
| `date` | Date only (no time component) | `Date` |
| `ip` | IP address (v4 or v6) | `IPv6` |
| `uuid` | Unique identifier (GUID, trace ID) | `UUID` |
| `json` | Structured/semi-structured data | `JSON` |
| `geo_point` | Latitude/longitude pair | `Point` |
| `enum` | Fixed set of allowed values (values defined in `default` field) | `Enum8(...)` |

### Why simplified primitives?

A data expert knows they have an "integer" column. They don't need to
decide between `Int8`, `Int16`, `Int32`, `Int64`, `UInt8`, `UInt16`,
`UInt32`, or `UInt64` -- that's 8 choices that require understanding
ClickHouse storage internals. The engine picks `Int64` because it covers
the vast majority of use cases without overflow risk.

If you genuinely need a narrower or unsigned type (performance-critical
high-volume tables), use `ch_override`:

```yaml
  - name: http_status
    type: integer
    ch_override: UInt16       # Override: HTTP status codes fit in UInt16
    use_case: dimension
```

### ClickHouse Type Override

When a primitive isn't sufficient, specify the exact ClickHouse type via
`ch_override`. When set:

- No automatic Nullable wrapping
- No automatic codec selection
- The type string is used verbatim in DDL
- Attributes are still applied if specified

```yaml
  - name: precision_ts
    ch_override: "DateTime64(6,'UTC')"
    comment: "Microsecond precision timestamp"

  - name: nested_data
    ch_override: "Nested(key String, value String)"

  - name: bitmap
    ch_override: "AggregateFunction(groupBitmap, UInt32)"
```

---

## ClickHouse Type Registry

The engine maintains a **canonical type registry** -- a YAML file that maps
each primitive to its ClickHouse implementation details. This is internal
to the engine (data SMEs don't edit it), but it's documented here for
completeness.

```yaml
# dfe-schemas registries/types.yaml -- NOT edited by schema authors
primitives:
  string:
    ch_type: String
    codec: ZSTD(1)
    nullable: true

  text:
    ch_type: String
    codec: ZSTD(3)
    nullable: true

  integer:
    ch_type: Int64
    codec: ZSTD(1)
    nullable: true

  float:
    ch_type: Float64
    codec: ZSTD(1)
    nullable: true

  boolean:
    ch_type: Bool
    codec: LZ4
    nullable: false

  datetime:
    ch_type: "DateTime64(3,'UTC')"
    codec: "Delta, ZSTD(1)"
    nullable: true

  timestamp:
    ch_type: "DateTime64(3,'UTC')"
    codec: "Delta, LZ4"
    nullable: false         # Never null -- used in ORDER BY

  date:
    ch_type: Date
    codec: "Delta, ZSTD(1)"
    nullable: true

  ip:
    ch_type: IPv6           # Stores both v4 and v6
    codec: LZ4
    nullable: true

  uuid:
    ch_type: UUID
    codec: ~                # No codec (already compact)
    nullable: true

  json:
    ch_type: JSON           # Native ClickHouse JSON (v25.3+)
    codec: ZSTD(3)
    nullable: true

  geo_point:
    ch_type: Point
    codec: ZSTD(1)
    nullable: true

  enum:
    ch_type: "Enum8(...)"   # Values from `default` field
    codec: ZSTD(1)
    nullable: false
```

### ClickHouse Type Overrides Catalogue

For users who need `ch_override`, these are the supported ClickHouse
types. The engine validates that `ch_override` values are in this
catalogue:

| Category | Types |
|----------|-------|
| **Integers** | `Int8`, `Int16`, `Int32`, `Int64`, `Int128`, `Int256` |
| **Unsigned** | `UInt8`, `UInt16`, `UInt32`, `UInt64`, `UInt128`, `UInt256` |
| **Floats** | `Float32`, `Float64` |
| **Decimal** | `Decimal(P,S)`, `Decimal32(S)`, `Decimal64(S)`, `Decimal128(S)` |
| **Strings** | `String`, `FixedString(N)` |
| **Dates** | `Date`, `Date32`, `DateTime`, `DateTime64(P)`, `DateTime64(P,'TZ')` |
| **Boolean** | `Bool` |
| **IP** | `IPv4`, `IPv6` |
| **UUID** | `UUID` |
| **JSON** | `JSON` |
| **Geo** | `Point`, `Ring`, `Polygon`, `MultiPolygon` |
| **Complex** | `Array(T)`, `Map(K,V)`, `Tuple(...)`, `Nested(...)` |
| **Enum** | `Enum8(...)`, `Enum16(...)` |
| **Special** | `Dynamic`, `Variant(...)`, `AggregateFunction(...)`, `SimpleAggregateFunction(...)` |

---

## Attribute Axis

Attributes modify how the type is stored. Specified as a **list** in
YAML -- multiple attributes can be combined:

| Attribute | What It Does |
|-----------|-------------|
| `lowcardinality` | Dictionary encoding -- huge performance gain for <10K distinct values |
| `nullable` | Allows NULL values (2x performance cost -- use only when NULL != empty) |
| `not_null` | Explicitly prevents NULL (overrides the primitive's default) |
| `materialized` | Column computed on insert, not stored in source data |
| `alias` | Virtual column computed at query time |

### Attributes Are Tied to Primitives

Like use cases, not every attribute applies to every type. The engine
enforces valid combinations:

| Attribute | Valid Primitives | Why |
|-----------|-----------------|-----|
| `lowcardinality` | `string`, `text`, `integer`, `float`, `date`, `ip` | Dictionary encoding only works on types with a finite value space. `json`, `geo_point` are not supported. |
| `nullable` | all | Any type can be nullable |
| `not_null` | all | Any type can be forced non-null |
| `materialized` | all | Computed on insert -- any type |
| `alias` | all | Computed at query time -- any type |

If you specify `lowcardinality` on a `json` column, the engine rejects it
at validation time.

### Nullability Defaults

Each primitive has a default nullability (defined in the type registry):

- **Nullable by default:** `string`, `text`, `integer`, `float`, `datetime`, `date`, `ip`, `uuid`, `json`, `geo_point`
- **NOT null by default:** `timestamp`, `boolean`, `enum`

Override with `nullable` or `not_null` in the attribute list.

### Why nullable is not the default for everything

ClickHouse stores `Nullable(T)` as two columns -- the data column plus a
UInt8 bitmap tracking which rows are null. This **doubles storage** and
**halves query speed** (measured: 229M rows/s -> 98M rows/s on GROUP BY
with Nullable(Int64)). Booleans and `timestamp` are non-null by default
for this reason.

**Design decision: ORDER BY keys must not be Nullable.** ClickHouse
refuses a Nullable column in a sorting key unless the merge-tree setting
`allow_nullable_key = 1` is enabled, and even with it on a null in the
sort key cripples index effectiveness. Rather than silently flip
`allow_nullable_key` on -- or fail the whole table -- the engine drops any
Nullable ORDER BY column from the sorting key and logs a warning. This
applies to `LowCardinality(Nullable(T))` too. To keep a column in the
ORDER BY, mark it `not_null` (or give it a DEFAULT, which also forces
non-null).

For payload columns where NULL genuinely means "not provided" (as opposed
to empty string or zero), Nullable is correct. Don't fight it -- just
keep it off your ORDER BY and high-filter columns.

### Examples

```yaml
# LowCardinality string, not nullable
  - name: category
    type: string
    attribute: [lowcardinality, not_null]
    use_case: dimension

# Multiple attributes
  - name: region_code
    type: string
    attribute: [lowcardinality, not_null]
    use_case: dimension

# Materialized column (computed from other columns at insert time)
  - name: day
    type: date
    attribute: [materialized]
    default: "toDate(_timestamp)"
```

---

## Use Case Axis

Use cases describe **how you query the column** -- not how it's stored.
The engine translates use cases into ClickHouse indexes and codecs in the
generated DDL. You don't need to know what a `set(0)` or
`tokenbf_v1(8192, 4, 0)` index is -- just pick the use case that matches
your query pattern.

### Use Cases

| Use Case | When to Use | Example Columns |
|----------|------------|-----------------|
| `dimension` | Filter by exact value: `WHERE status = 'error'` | status, severity, region, org_id, category |
| `fulltext` | Search words in log messages: `WHERE hasToken(message, 'error')` | message, log_body, description |
| `text_search` | Substring search: `WHERE message LIKE '%connection refused%'` | syslog_message, windows_event_data |
| `range` | Numeric/time ranges: `WHERE latency > 100` | latency_ms, timestamp, bytes, risk_score |
| `bloom` | Find specific IDs in high-cardinality columns | trace_id, request_id, span_id |
| _(empty)_ | No special query optimisation needed | raw payload, metadata |

### Use Cases Are Tied to Primitives

Not every use case makes sense on every type. The engine enforces valid
combinations:

| Use Case | Valid Primitives | Why |
|----------|-----------------|-----|
| `dimension` | `string`, `integer`, `boolean`, `enum`, `ip`, `uuid` | Exact match -- needs discrete values |
| `fulltext` | `string`, `text` | Token search -- only applies to text |
| `text_search` | `string`, `text` | Substring matching -- only applies to text |
| `range` | `integer`, `float`, `datetime`, `timestamp`, `date`, `ip` | Range queries -- needs orderable values |
| `bloom` | `string`, `uuid` | Point lookups on high-cardinality identifiers |

If you specify `fulltext` on an `integer` column, the engine rejects it
at validation time with a clear error.

### What the Engine Generates in the DDL

You don't need to know this to use the schema system. This section is for
engine developers and anyone curious about what happens behind the scenes.

| Use Case | ClickHouse Index Generated | Granularity | Notes |
|----------|---------------------------|-------------|-------|
| `dimension` | `set(0)` | 4 | Exact distinct values per granule |
| `fulltext` | `text(tokenizer=splitByNonAlpha)` | 1 | Native text index (GA v26.2). Deterministic, no false positives, row-level filtering. 45x faster than without index. |
| `text_search` | `text(tokenizer=ngrams(3))` | 1 | Character n-gram text index for substring matching |
| `range` | `minmax` | 4 | Stores min/max per granule |
| `bloom` | `bloom_filter` | 4 | Probabilistic -- has false positives, no false negatives |
| _(empty)_ | No index | -- | |

**Note on text indexes:** The `fulltext` and `text_search` use cases now
generate the GA text index (inverted index, v26.2+) instead of the older
bloom-filter based `tokenbf_v1` and `ngrambf_v1`. The text index is
deterministic (no false positives), provides row-level filtering instead
of granule-level, and is 10-100x faster for text search workloads. For
ClickHouse versions before v25.10, the engine falls back to the legacy
bloom-filter indexes automatically.

**Note on fulltext vs text_search:** Both use the text index but with
different tokenizers. `fulltext` uses word-level tokenization
(`splitByNonAlpha`) -- good for searching whole words in log messages.
`text_search` uses character n-grams -- good for substring matching like
partial hostnames or error codes embedded in longer strings.

**Retention (TTL):** every time-series table gets a TTL, 90 days unless the
deployment sets `DFE_CLICKHOUSE_DEFAULT_TTL_DAYS` (`clickhouse.default_ttl_days`,
0 = no default). Precedence is the source's `schema.ttl_days`, then the table's
own dfe-schemas definition, then the deployment default. State tables (the
detection checkpoint, the engine's internal and hunt-coordination tables) keep
whatever their definition declares and never take the default. The schema
apply reconciles TTL as well as columns, so a table that already exists follows
a changed default on the next apply (`ALTER TABLE ... MODIFY TTL`), and an
undeclared TTL never removes a live one. Shortening a TTL expires the rows
older than the new value.

**Console override:** an operator can change the deployment default without a
redeploy through `PUT /api/v1/system/retention` (`system:write`), and
`GET /api/v1/system/retention` reports the stored value, the effective value
and which one wins. The override is committed to the deploy repo at
`governance/settings/retention.yaml` (`default_ttl_days`), so it survives the
loss of the engine and every change is an audited commit. Full precedence is
the source's `schema.ttl_days`, then the table's dfe-schemas definition, then
the console override, then the deployment env default. A PUT reconciles the
core tables and every deployed source's table in the same request, and the
engine's own startup apply uses the override too. The deploy-time `dfe-schema`
CLI has no gitops access and applies the env default only, so `dfe-schema
check` reports an active override as TTL drift.

---

## Comment Axis: Loader Directives

The `comment` field serves double duty:

1. **Human documentation** -- plain text description
2. **Loader directives** -- `@` prefixed expressions that tell dfe-loader
   how to populate the field

### Directive Types (from dfe-loader DDL Expression Language)

| Directive | Purpose | Example |
|-----------|---------|---------|
| `@source: field` | Copy from source data | `@source: timestamp \| now()` |
| `@source: first(a/b/c)` | First non-null from list | `@source: first(user_id/uid/id)` |
| `@generated: expr` | ClickHouse DEFAULT -- loader omits field | `@generated: now64(3)` |
| `@renamed: field` | Zero-copy field rename | `@renamed: logoriginal` |
| `@captured: payload` | Raw payload sidecar | `@captured: raw_payload as JSON` |
| `@computed: expr` | Derived/enriched value | `@computed: geoip(ip).country_code` |

### Precedence

When the loader resolves field mappings, column comments have **highest
precedence** -- above built-in presets and external remap files. This means
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
build its per-table field mapping. No separate mapping config needed -- the
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
| **passthrough** | `_timestamp_load`, `_uuid`, `_org_id`, `_json` | Transparent bridge -- no timestamp injection |

### Common Header Names Are Reserved

A common header name belongs to the pipeline, not to the payload. When an
incoming record already carries one of these names at the top level, the
ingest side renames the payload's value to `<name>_original` and stamps its
own value under the name itself -- the loader routes on `_source`, and two
top-level keys of the same name make ClickHouse reject the whole record
rather than dead-letter it. `<name>_original` is never overwritten: a record
arriving with both the name and its `_original` (a replay, for instance)
keeps the `_original` it came with, and the colliding value is parked under
the next free `<name>_original_<n>` counting from 2.

For a schema author this means two things. A promoted column named after a
common header field will not receive the payload's value, and a payload field
you want to keep must be given a different name -- or read back from
`<name>_original`, which is a dynamic path in `_json` unless you promote it
explicitly.

### Profile Tied to Source

Each Source selects a profile via the `header.type` field:

```yaml
# sources/filebeat.yaml
header:
  type: timeseries              # Profile name
  version: 1.0.0                # Common header version (semver)
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
COLUMN` -- safe, non-destructive, and automated.

---

## Sigma Column Mappings

Sigma rules reference fields by standardised names (e.g. `SourceIP`,
`CommandLine`, `EventID`). These must map to actual ClickHouse column names.

### Approach: ClickHouse View per Sigma Taxonomy

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

The Source definition declares its naming-standard views in the generic
`views` list; the sigma entry carries the logsource binding and overrides:

```yaml
# sources/windows_audit.yaml
views:
  - standard: sigma
    taxonomy: windows                   # Sigma logsource product binding
    custom_mappings:                    # Per-source overrides (win over field_map)
      CommandLine: command_line
      ParentCommandLine: parent_cmd
```

The engine generates the Sigma view from source schema + the sigma view entry.

---

## Elastic Index Template Converter

The existing Elastic/OpenSearch index template converter generates a
source meta schema from a supplied Elastic template JSON. This remains --
but now outputs YAML in the Source schema format:

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
Source schema YAML (new format)
  │  name, type, attribute, use_case, comment
  │
  ▼
Attached to a Source definition
```

### Elastic -> Primitive Type Mapping

| Elastic Type | Primitive | Use Case | Notes |
|-------------|-----------|----------|-------|
| `keyword` | `string` | `dimension` | Exact match filtering |
| `text` | `text` | `fulltext` | Full-text search |
| `long` | `integer` | | Default Int64 matches |
| `integer` | `integer` | | |
| `short` | `integer` | | ch_override: Int16 if needed |
| `byte` | `integer` | | ch_override: Int8 if needed |
| `double` | `float` | | |
| `float` | `float` | | ch_override: Float32 if needed |
| `boolean` | `boolean` | | |
| `date` | `datetime` | | |
| `ip` | `ip` | `range` | |
| `geo_point` | `geo_point` | | |
| `object` | `json` | | |
| `nested` | `json` | | |

The converter is invoked when creating a Source from an existing Elastic
data source -- it bootstraps the schema so the user doesn't start from
scratch.

---

## Source-Scoped Entities

Beyond the schema itself, several other entities are scoped to a Source.
See [source-definition.md](source-definition.md) for the full Source definition.

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

Rules reference `{db}`, `{source}`, `{from}`, `{to}` -- templated at
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
- Source determined by the Sigma `logsource` -> Source mapping
- Sigma view used for field name compatibility

---

## Migration from v1 Type System

### Type Mapping (old -> new)

| Old Type | New Primitive | Attribute | Notes |
|----------|--------------|-----------|-------|
| `string` | `string` | | |
| `string_fast` | `string` | | Codec auto-selected |
| `string_lowcardinality` | `string` | `lowcardinality` | |
| `string_fast_lowcardinality` | `string` | `lowcardinality` | |
| `text` | `text` | | |
| `json` | `json` | | |
| `int8` | `integer` | | ch_override: Int8 |
| `int16` | `integer` | | ch_override: Int16 |
| `int32` | `integer` | | ch_override: Int32 |
| `int64` | `integer` | | Default -- no override needed |
| `int128` | `integer` | | ch_override: Int128 |
| `int256` | `integer` | | ch_override: Int256 |
| `float32` | `float` | | ch_override: Float32 |
| `float64` | `float` | | Default -- no override needed |
| `boolean` | `boolean` | | |
| `timestamp` | `timestamp` | | |
| `datetime` | `datetime` | | |
| `ipv4` | `ip` | | ch_override: IPv4 if v4-only needed |
| `ipv6` | `ip` | | Default IPv6 stores both |
| `ip_field` | `ip` | | |
| `geo_point` | `geo_point` | | |
| `uuid` | `uuid` | | |
| `tuple` | _(use ch_override)_ | | `ch_override: Tuple(...)` |
| `map` | _(use ch_override)_ | | `ch_override: Map(K,V)` |

### Index Type Mapping

| Old `index_type` | New `use_case` | Notes |
|-----------------|----------------|-------|
| `dimension` | `dimension` | Same |
| `fulltext` | `fulltext` | Now generates text index (was tokenbf_v1) |
| `text_search` | `text_search` | Now generates ngram text index (was ngrambf_v1) |
| `hc` | `bloom` | Renamed for clarity |
| `range` | `range` | Same |
| `minmax` | `range` | Merged (both generated minmax) |

### Format Change

```
# Old: CSV
# column,type,default,index_order,index_type,comment

# New: YAML
# columns:
#   - name: ...
#     type: ...
#     attribute: [...]
#     use_case: ...
#     default: ...
#     order: ...
#     comment: ...
```

The migration is mechanical -- a script maps old compound types to new
primitives (+ ch_override where the old type was narrower than the
default), renames `index_type` to `use_case`, and converts CSV to YAML.
