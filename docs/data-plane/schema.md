# Schema Type System v2

The schema YAML SSoT is the dfe-schemas repo (the version-pinned `dfe-schemas`
wheel), registries included, and the engine keeps no copies.

---

## Audience

This document is written for **data subject matter experts** -- people who
understand their data, their queries, and their retention requirements. You
do NOT need to be a ClickHouse expert. The schema system picks the codecs,
indexes, Nullable wrapping and type widths. You pick meaningful types and describe how your data will be queried.
The engine does the rest.

If you need a specific ClickHouse type that the primitives don't cover,
there's an escape hatch (`ch_override`). But you shouldn't need it for 95%
of columns.

---

## Design Principles

### A meta schema instead of a schema registry

A schema registry asks one question of every record at the door: does it match a registered shape, and which version. DFE does not ask it. Every record is accepted as it arrives and lands whole in `_json` under the common header, so nothing is rejected or versioned on the way in. The shape question moves up one level. The common header carries what every table needs (timestamps, source, org, tags, partitioning, TTL), a meta schema per source names the fields worth a real column, and the engine turns the two into DDL. A field the meta schema does not name is still in `_json`, queryable, and can be promoted to a column later with no reindex ([data-evolution](../data-evolution.md)).

So there is **no schema registry** -- no CRUD service, no sync state, no
intermediate store. Two sources of truth exist, and that's it:

1. **Source YAML** -- the meta schema definition (what the schema should be)
2. **ClickHouse DDL** -- the deployed table (what the schema actually is)

The engine reads the Source YAML, generates DDL, and applies it to
ClickHouse. The loader reads the deployed table's columns and comments
from `system.columns` at runtime. There is nothing in between.

Schema changes flow in one direction:

```text
Source YAML (intent) -> engine generates DDL -> ClickHouse (deployed truth)
                                                     ^
                                        dfe-loader reads it at runtime
```

dfe-loader reads column types, comments and table metadata from
`system.columns` at runtime, never from the YAML. It caches them for
`schema.cache_ttl_secs`, so an applied change reaches it on the next refresh
([schema-sync.md](schema-sync.md)).

### Schema Format: YAML

Schema definitions are **YAML**. YAML supports structured data
(lists of attributes, nested overrides) and is consistent with the rest
of the Source definition format.

**CSVs remain only for enrichment files** used by transform stages
(e.g. GeoIP lookups, threat intelligence feeds, asset inventories).
These are data files, not schema definitions.

### Schema Lifecycle

| Event | Action |
|-------|--------|
| Source created | Engine generates `CREATE TABLE` from common header + source schema |
| Schema field added | Engine generates `ALTER TABLE ADD COLUMN` |
| Index or TTL changed | The schema apply drops and re-adds the index, or moves the TTL. A changed column type is drift, applied only with `dfe schema apply --allow-drift` ([schema-bootstrap.md](schema-bootstrap.md)) |
| Source disabled | No DDL change -- table stays, no new data |
| Source deleted | Table preserved -- manual `DROP TABLE` if needed |

The deployed ClickHouse table is authoritative. A change made to it directly is
reported as drift by the next schema plan or apply.

---

## The axes

A schema column definition has independent axes, each optional where it has a
default, plus the loader directive in `expr`:

```text
type + cardinality + attribute + use_case, with expr (the loader directive) and comment (prose)
```

---

## Schema Definition Format (YAML)

The common header columns come from the profile (`common-header/` in
dfe-schemas, see "Common Header Profiles" below). A meta schema declares the rest:

```yaml
# meta_schema.yaml
columns:
  - name: user_name
    type: string
    use_case: dimension
    expr: "@source: first(user_id/uid/id)"
    comment: "User identifier"

  - name: source_ip
    type: ip
    use_case: range
    expr: "@source: src_ip"

  - name: severity
    type: string
    cardinality: low
    use_case: dimension

  - name: message
    type: text
    use_case: word_search

  - name: event_id
    type: integer
    expr: "@source: event_id"

  - name: latency_ms
    type: float
    use_case: range
```

### Column Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Column name |
| `type` | Yes | Primitive type (see below) |
| `cardinality` | No | `low`, `high` or `unknown` (the default) -- see below |
| `attribute` | No | List of storage attributes (e.g. `[not_null]`) |
| `use_case` | No | Query pattern hint -- determines indexing (see below) |
| `default` | No | DEFAULT expression |
| `order` | No | Position in ORDER BY / PRIMARY KEY |
| `expr` | No | The loader directive (`@source: ...`), see below |
| `comment` | No | Human description. Never a directive |
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
| `map` | String keys to string values, never null | `Map(LowCardinality(String), String)` |
| `vector` | Embedding of floats, never null | `Array(Float32)` |

### Why simplified primitives?

A data expert knows they have an "integer" column. They don't need to
decide between `Int8`, `Int16`, `Int32`, `Int64`, `UInt8`, `UInt16`,
`UInt32`, or `UInt64` -- that's 8 choices that require understanding
ClickHouse storage internals. The engine picks `Int64` because it covers
the vast majority of use cases without overflow risk.

If you need a narrower or unsigned type (performance-critical
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

  map:                      # ClickHouse refuses Map and Array inside Nullable
    ch_type: "Map(LowCardinality(String), String)"
    codec: ZSTD(1)
    nullable: false

  vector:
    ch_type: "Array(Float32)"
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

`GET /api/v1/schemas/vocabulary` serves what a schema form offers, read from this registry: the primitives, the index use cases with their labels and the names they replaced, the attributes, the cardinalities with their default, and the override types with the primitive each refines. A type that takes parameters carries a `template` such as `Decimal({precision},{scale})`, so a form builds the override text without knowing the type. A label the registry does not declare reads off the entry's name. The retired `lowcardinality` attribute is not offered: a column declares `cardinality`, and the vocabulary says a stored `lowcardinality` reads as `low`.

---

## Cardinality Axis

How many distinct values the column holds. One declaration, because it decides
two things that must agree: the storage wrapper, and whether `exact_match`
chooses `set(0)` or `bloom_filter`.

| Value | Storage | `exact_match` index |
|-------|---------|---------------------|
| `low` | `LowCardinality(...)` -- dictionary encoding, which pays below about 10K distinct values | `set(0)`, holding every distinct value exactly |
| `high` | plain | `bloom_filter`, bounded and probabilistic |
| `unknown` *(default)* | plain | `bloom_filter` |

`unknown` is the honest default. Nobody re-reviews a field that already looks
decided, so a column with nothing measured says so and gets the safe way to be
wrong.

An Elasticsearch index template does not carry cardinality, so an imported
column is `unknown`.

`attribute: [lowcardinality]` is the retired spelling and still reads as
`cardinality: low`. Declaring the two against each other is refused.

---

## Attribute Axis

Attributes modify how the type is stored. Specified as a **list** in
YAML -- multiple attributes can be combined:

| Attribute | What It Does |
|-----------|-------------|
| `lowcardinality` | Retired -- the old spelling of `cardinality: low` |
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

- Nullable by default: `string`, `text`, `integer`, `float`, `datetime`, `date`, `ip`, `uuid`, `json`, `geo_point`
- NOT null by default: `timestamp`, `boolean`, `enum`, `map`, `vector`

Override with `nullable` or `not_null` in the attribute list.

### Why nullable is not the default for everything

ClickHouse stores `Nullable(T)` as two columns -- the data column plus a
UInt8 bitmap tracking which rows are null -- so it costs storage and query
speed. Booleans and `timestamp` are non-null by default for this reason.

**Design decision: ORDER BY keys must not be Nullable.** ClickHouse
refuses a Nullable column in a sorting key unless the merge-tree setting
`allow_nullable_key = 1` is enabled, and even with it on a null in the
sort key cripples index effectiveness. Rather than silently flip
`allow_nullable_key` on -- or fail the whole table -- the engine drops any
Nullable ORDER BY column from the sorting key and logs a warning. This
applies to `LowCardinality(Nullable(T))` too. To keep a column in the
ORDER BY, mark it `not_null` (or give it a DEFAULT, which also forces
non-null).

For payload columns where NULL means "not provided" (as opposed
to empty string or zero), Nullable is correct. Don't fight it -- just
keep it off your ORDER BY and high-filter columns.

### Examples

```yaml
# LowCardinality string, not nullable
  - name: category
    type: string
    cardinality: low
    attribute: [not_null]
    use_case: dimension

# Multiple attributes
  - name: region_code
    type: string
    cardinality: low
    attribute: [not_null]
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

A use case names the question you ask the column, never the ClickHouse index
that answers it. You declare the question. The engine picks the index, and can
pick a different one on a later ClickHouse without your schema changing.

| Use Case | The question | Example Columns |
|----------|------------|-----------------|
| `dimension` | I filter and group by this: `WHERE status = 'error'` | status, severity, region, org_id, category |
| `exact_match` | I look up specific values | trace_id, request_id, span_id |
| `range` | I query ranges, between, time windows: `WHERE latency > 100` | latency_ms, timestamp, bytes, risk_score |
| `word_search` | I search for whole words: `WHERE hasToken(message, 'error')` | message, log_body, description |
| `substring_search` | I search for fragments inside words: `WHERE message LIKE '%connection refused%'` | syslog_message, windows_event_data |
| `key_search` | I search the keys and values of a map column | attributes, labels |
| `similarity_search(<dims>)` | I find records similar to this one | embedding |
| *(empty)* | No index | raw payload, metadata |

`similarity_search` is the one use case that takes an argument: ClickHouse needs
the vector's dimension count up front and cannot read it off the column. You
give it that, and the engine still picks the method and the distance metric.

A field you query often belongs in a typed column with a use case, not inside
`_json`. `_json` stores each value with its own type, row by row, so a path that
holds both strings and numbers breaks `=`, `IN` and the string functions at
query time, and no `_json` path has an index. Promoting the field fixes both:
see [Data evolution](../data-evolution.md#promote-see-it-then-give-it-a-column).

### Use Cases Are Tied to Primitives

Not every use case makes sense on every type. The engine enforces valid
combinations:

| Use Case | Valid Primitives | Why |
|----------|-----------------|-----|
| `dimension` | `string`, `integer`, `boolean`, `enum`, `ip`, `uuid` | Exact match -- needs discrete values |
| `exact_match` | `string`, `uuid`, `integer`, `ip` | Point lookups on high-cardinality identifiers, numeric ids and addresses |
| `range` | `integer`, `float`, `datetime`, `timestamp`, `date`, `ip` | Range queries -- needs orderable values |
| `word_search` | `string`, `text` | Token search -- only applies to text |
| `substring_search` | `string`, `text` | Substring matching -- only applies to text |
| `key_search` | `map` | The keys and values of a map column |
| `similarity_search` | `vector` | Nearest neighbours in an embedding space |

If you specify `word_search` on an `integer` column, the engine rejects it
at validation time with a clear error.

### What the Engine Generates in the DDL

You don't need to know this to use the schema system. This section is for
engine developers and anyone curious about what happens behind the scenes. It
is what the engine emits today, on the ClickHouse the stack pins -- not part of the
vocabulary.

| Use Case | ClickHouse Index Generated | Granularity | Notes |
|----------|---------------------------|-------------|-------|
| `dimension` | `set(0)`, or `bloom_filter` on a Nullable column | 4 | Exact distinct values per granule |
| `exact_match` | `set(0)` on `cardinality: low`, else `bloom_filter` | 4 | The bloom filter is probabilistic -- false positives, no false negatives |
| `range` | `minmax` | 4 | Stores min/max per granule |
| `word_search` | `text(tokenizer=splitByNonAlpha)` | 1 | Native text index. Deterministic, no false positives, row-level filtering |
| `substring_search` | `text(tokenizer=ngrams(3))` | 1 | Character n-gram text index for substring matching |
| `key_search` | two indexes, `text(tokenizer=array)` over `mapKeys(col)` and `mapValues(col)` | 1 | A text index refuses a `Map` column itself, so the keys and the values are indexed apart |
| `similarity_search(<dims>)` | `vector_similarity('hnsw', 'cosineDistance', <dims>)` | 100000000 | One index per part. `hnsw` is the only method the pinned ClickHouse implements |
| *(empty)* | No index | -- | |

**Note on text indexes:** `word_search` and `substring_search` generate the GA
text index instead of the older bloom-filter based `tokenbf_v1` and
`ngrambf_v1`. The text index is deterministic (no false positives) and filters
at row level rather than granule level. Every table gets the text index.

Every index, partition and TTL clause comes from dfe-schemas, which owns the one
table renderer and the DDL catalogue (`registries/ddl.yaml`). The engine builds
a `TableSpec` from the source and calls it.

An index whose answer changes is dropped and added again on the next apply. The new index covers parts written or merged after that, and existing parts gain it on merge: there is no `MATERIALIZE INDEX`.

**Note on word_search vs substring_search:** Both use the text index but with
different tokenizers. `word_search` uses word-level tokenization
(`splitByNonAlpha`) -- good for searching whole words in log messages.
`substring_search` uses character n-grams -- good for fragments inside longer
strings, like partial hostnames or embedded error codes.

**Retention (TTL):** every time-series table gets a TTL, 90 days unless the
deployment sets `DFE_ENGINE_CLICKHOUSE_DEFAULT_TTL_DAYS` (`clickhouse.default_ttl_days`,
0 = no default) or an admin sets a console value (below). Precedence is the source's
`schema.ttl_days`, then the table's own dfe-schemas definition, then the console
value, then the deployment default. State tables (the
detection checkpoint, the engine's internal and hunt-coordination tables) keep
whatever their definition declares and never take the default. The schema apply refuses a changed TTL on one of the engine's own tables as drift rather than expiring rows at boot. An undeclared TTL never removes a live one. Shortening a TTL expires the rows
older than the new value.

**Changing the default:** an admin sets it with `PUT /api/v1/system/retention` (`system:write`, which only the `admin` role holds by default). `GET` reports the effective value, the console value and the deployment value. The console value is committed to the deploy repo at `governance/settings/retention.yaml` (`default_ttl_days`), so it survives the loss of the engine and every change is an audited commit. `null` clears it. The PUT applies the new value in the same request to every table that follows the default, the engine's own time-series tables and each deployed source's table, with `ALTER TABLE ... MODIFY TTL` (`REMOVE TTL` for 0). Every later build, plan, deploy and boot reads the same value.

On a restart the engine brings every deployed source's table to the effective default, but the schema apply refuses a changed default on its own tables, so after changing `DFE_ENGINE_CLICKHOUSE_DEFAULT_TTL_DAYS` send the PUT with `null` to apply it there. `dfe schema plan` and `dfe schema apply` read the deployment settings, never the deploy repo, so while a console value is set they report the engine's time-series tables as TTL drift, and `dfe schema apply --allow-drift` rewrites those TTLs to the deployment default.

---

## Loader directives (`expr`)

A column's `expr` carries the `@` directive that tells dfe-loader how to fill it,
and `comment` the human description. Both land in the ClickHouse column COMMENT,
joined by ` - `.

### The directives dfe-loader parses

dfe-loader's parser (`src/column_meta/mod.rs`) knows six directives and skips any
other word:

| Directive | Purpose | Example |
|-----------|---------|---------|
| `@source: field` | Copy from source data, with an optional `\| fallback` | `@source: timestamp \| now()` |
| `@source: first(a/b/c)` | First present field from a list | `@source: first(user_id/uid/id)` |
| `@renamed: field` | Source field path(s), the hand-written form of `@source` | `@renamed: logoriginal` |
| `@computed: expr` | A CEL expression producing the value | `@computed: risk_score > 80` |
| `@default: value` | Substitute when the field is null or absent | `@default: unknown` |
| `@skip` | Leave the column out of the insert | `@skip` |
| `@coerce: category` | Override the type category for coercion | `@coerce: <category>` |

dfe-schemas also writes `@generated: <expr>` on a column ClickHouse fills from
its DEFAULT, such as `_uuid`. The loader does not parse it.

### Precedence

The loader's config cascade wins over a column COMMENT: per-table
`column_directives`, then global `column_directives`, then the COMMENT
(dfe-loader `src/column_meta/mod.rs`).

### Comments in DDL

These directives are emitted as ClickHouse column comments in the generated
DDL:

```sql
`_timestamp` DateTime64(3,'UTC') CODEC(Delta, LZ4)
    COMMENT '@source: timestamp | now() - Event timestamp from source data',
`user_name` Nullable(String) CODEC(ZSTD(1))
    COMMENT '@source: first(user_id/uid/id) - User identifier',
```

The loader fetches column comments from `system.columns` and builds its
per-table field mapping from them, under whatever its config cascade sets.

---

## Common Header Profiles

The common header is not one-size-fits-all. Different data types need
different system fields. Profiles define which common header fields to
inject:

| Profile | Fields | Use Case |
|---------|--------|----------|
| **timeseries** (default) | `_timestamp_load`, `_timestamp`, `_timestamp_received`, `_uuid`, `_org_id`, `_source`, `_raw`, `_json`, `_tags` | Full event ingestion (logs, alerts, audit) |
| **minimal** | `_timestamp_load`, `_timestamp`, `_uuid`, `_org_id`, `_json` | High-volume structured data (metrics, flow records) |
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
COMMENT '@profile: timeseries | @profile_version: <header version> | @<table>_version: <schema version>'
```

The engine writes these tags on a source table (`DDLGenerator._table_comment`),
and dfe-schemas the first two on a header-composed manifest table.

Profile migrations (adding new common header fields) use `ALTER TABLE ADD
COLUMN` -- safe, non-destructive, and automated.

---

## The `all_sources` Merge Table

`dfe.all_sources` is a ClickHouse Merge table over `main` and each registered source's MergeTree table, so one Observe source reads every source's rows. Core manifest tables other than `main` (even under a source's name) and tables without `_org_id` are never members.

It declares `_timestamp_load`, `_timestamp`, `_timestamp_received`, `_org_id`, `_source`, `_raw` and `_json`, typed as on `main`. A member lacking one reads its default: epoch 0 for a timestamp, NULL for a Nullable column. `_raw IS NULL` misreads a member without `_raw`, since ClickHouse answers it from the `_raw.null` subcolumn while `optimize_functions_to_subcolumns` is on. Test `length(_raw)` or use `coalesce`. The virtual column `_table` names each row's member.

The engine rebuilds it after every source deploy or delete. At startup it rebuilds once the [schema bootstrap](schema-bootstrap.md) converges or is switched off, never after a failed pass. Each rebuild reads the registry as it stands then. Concurrent rebuilds in one engine fold into one. A deleted source leaves the members while its table stays.

Each rebuild applies the tenant row policy ([rbac.md](../control-plane/rbac.md)) to `all_sources` itself. Member tables keep their own policies, which also fence a read through it.

The engine registers it on every team as the Observe source `all_sources`, timed on `_timestamp_load` because `_timestamp` reads as epoch 0 for a passthrough member. Registration retries with backoff from 60 s to 15 min until dfe-hyperdx accepts it.

## The `dlq` Table

`dfe.dlq` holds the dead letters every app writes, kept 30 days. The engine registers it on every team as the HyperDX source `dlq`, timed on `_timestamp`, alongside `all_sources` and with the same retry. A source cannot take the name of a table the core manifest owns, such as `dlq` or `detection`, since its table would be that core table.

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
- Mappings resolved by `src/dfe_engine/sigma/source_mapper.py` from the source's field map and sigma view

### Sigma Mapping as Source Metadata

The Source definition declares its naming-standard views in the generic
`views` list. The sigma entry carries the logsource binding and overrides:

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

The Elastic/OpenSearch index template converter
(`src/dfe_engine/services/schema/elastic_schema_service.py`) generates a source
meta schema from a supplied Elastic template JSON, as YAML in the Source schema
format:

```text
Elastic Template JSON
  │
  ▼
Template Converter (dfe-engine)
  │  Maps Elastic types -> primitives
  │  Maps Elastic analyzers -> use_cases
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
| `keyword`, `wildcard`, `version` | `string` | | No use case: cardinality is unknown |
| `constant_keyword` | `string` | `dimension` | Bounded by definition |
| `text`, `match_only_text` | `text` | `word_search` | Searching for whole words |
| `long`, `integer`, `short`, `byte`, `unsigned_long` | `integer` | `range` | ch_override narrows if needed |
| `double`, `float`, `half_float`, `scaled_float` | `float` | `range` | |
| `boolean` | `boolean` | `dimension` | |
| `date`, `date_nanos` | `datetime` | `range` | |
| `ip` | `ip` | `range` | |
| `geo_point` | `geo_point` | | |
| `object`, `nested`, `flattened`, anything else | `json` (nullable) | | |

The converter is invoked when creating a Source from an existing Elastic
data source -- it bootstraps the schema so the user doesn't start from
scratch.

---

## Source-Scoped Entities

Beyond the schema itself, several other entities are scoped to a Source.
See [source-definition.md](source-definition.md) for the full Source definition.

### Rules and hunts

A **Rule** is a detection condition (a SQL WHERE fragment or a CEL filter) on a
source's table, so it sees exactly the columns the source's schema defines. A
**Hunt** groups rules, names the customers whose rows they read, and carries the
interval schedule. Matches land in `<data database>.detection` unless the hunt or
rule names another table. Current rule and hunt files:
[source-detections.md](source-detections.md).

### Sigma Rules (converted)

Sigma rules are converted to DFE Rules by propagation (`dfe_engine.sigma.propagation`, `convert_detection_to_where`).
The conversion produces a Rule definition with:

- SQL query generated from the Sigma detection logic
- Source determined by the Sigma `logsource` -> Source mapping
- Sigma view used for field name compatibility
