<!--
  Project:   dfe-engine
  File:      docs/data-plane/source-definition.md
  Purpose:   The source YAML field by field, and what a write is validated against
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# The source definition

One YAML file per source, and it carries the whole stream: identity, where the
records come from, whether they are transformed, the table they land in, and
the standard views they expose. What a Source IS lives in
[source.md](source.md); this is the file itself.

## A full source

```yaml
# Example: sources/filebeat.yaml
source: filebeat                        # The _source label -- immutable identifier
display_name: Filebeat
description: Elastic Filebeat log collector
state: active                           # active | dormant | disabled (see source-registry.md)

# --- Flow (optional; see source-flow.md) ---
transport: bus                          # bus (a broker holds records between stages) | direct (gRPC,
                                        # nothing stored). Omit to take the deployment default
archive: false                          # Keep the raw record as it arrived. Needs the bus transport:
                                        # the archiver reads the landing topic, and direct has none

# --- Header ---
# Common schema header. A new source starts as JUST this header -- the common
# fields for the selected type and version. Source-specific fields are added
# incrementally.
header:
  type: timeseries                      # Profile name (timeseries, minimal, passthrough)
  version: 1.0.0                        # Common header version (semver)

# --- Origin: receiver match OR fetcher (exactly one) ---
# A receiver-based source is identified by the always-present receiver pool:
# the receiver evaluates match rules and sets _source in the JSON payload.
match:
  field: tags.collector.type            # Dotted path into the incoming JSON payload
  operator: equals                      # equals (default) | exists - the receiver-evaluable set
  value: filebeat                       # Operand (unused when operator is exists or always)
  # The model also defines not_equals / includes / starts_with / ends_with,
  # but the receiver's hot-path router cannot evaluate them (a documented
  # receiver gap) - saves reject them for any non-disabled source.
  # `always` matches every record and is reserved for the `main` source,
  # which is how the main flow is defined - see source-flow.md.

# A fetcher-based source has no match rule. The engine deploys one dfe-fetcher
# instance named for the source, with this stanza compiled into it, when the
# source is active and deployed, and removes it when the source is not.
# fetcher:
#   source_type: okta                   # A family the deployed fetcher ships (apps.yaml source_types)
#   topic: own                          # own: this source's topic and table | main: the shared main topic and table
#   config:                             # The fetcher's own per-type stanza, verbatim
#     tenant_url: https://example.okta.com
#     credential_secret: vault:secret/dfe/okta:token   # credentials are env:/vault: references, never literals
#     services:
#       - name: system_log
#     interval_secs: 300
#   routes:                             # Fetched records that belong to ANOTHER source
#     - match: {field: eventType, operator: starts_with, value: policy.}
#       source: okta-policy             # Their landing, table and transform, not this source's

# --- Topics (derived, not configured) ---
# topic_land: filebeat_land             # Auto: {_source}_land
# topic_load: filebeat_load             # Auto: {_source}_load (only if transform exists)

# --- Transform (optional) ---
# If present, records flow _land -> transform -> _load; if absent, _land -> loader.
transform:
  engine: vector                        # A catalogued transform app, by engine name (apps.yaml)
  variant: filebeat.okta.default        # The compiled-in program, where the app offers a catalogue
  config_file: /etc/vector/filebeat.yaml
  env: {}                               # Per-transform ENV overrides
  files: []                             # Enrichment files (CSV, MMDB)

# --- Views (optional) ---
# Naming-standard views this source exposes (sigma, ecs, cim, ocsf).
# One entry per standard; inline custom_mappings WIN over the field_map pin.
views:
  - standard: sigma
    field_map: sigma/windows            # FieldMap registry pin ("standard/name" or bare name)
    taxonomy: windows                   # Sigma logsource product binding (sigma views only)
    custom_mappings:                    # Per-source overrides (win over field_map)
      CommandLine: command_line

# --- Schema (mandatory) ---
# The source owns its ClickHouse table schema: one source = one table = one schema.
schema:
  meta_schema: logs_beats_filebeat      # Base field definitions (YAML)
  meta_schema_version: 1.0.0
  derived_schema: filebeat/derived      # Source-specific field overrides (optional)
  additional_fields: filebeat/add       # Extra fields, indexes (optional)
  ttl_days: 90                          # Data retention
  engine: MergeTree                     # Base variant only (MergeTree, ReplacingMergeTree(...), ...)
                                        # Topology (Replicated/Shared/Cloud) resolves at DDL time
```

`match.field` is a dotted path into the record as it arrives, which is what the
receiver's router splits on. It is not a ClickHouse column reference: a
`_json.` prefix would be read as a first path segment that no payload has.

`ttl_days` is optional. A source that leaves it unset gets the deployment
default (`DFE_CLICKHOUSE_DEFAULT_TTL_DAYS`, 90 days shipped, 0 = none); a value
here wins over that default and over the table's dfe-schemas definition. The
next schema apply moves an existing table onto a changed value with `ALTER
TABLE ... MODIFY TTL`, and shortening it expires the rows older than the new
value.

The 2.1 keys `sigma`, `mapping_standards` and `field_mappings` were a clean
break in 2.2, not a deprecation -- a write carrying one is rejected with a
"removed in 2.2" error. Declare naming-standard views under `views` instead.

## The smallest source that works

```yaml
source: syslog
display_name: Syslog
match:
  field: tags.collector.type
  value: syslog
header:
  type: timeseries
  version: 1.0.0
schema:
  ttl_days: 90
```

No fetcher, no transform. The receiver matches and routes to `syslog_land`, the
loader picks it up and inserts into `{db}.syslog` using the common timeseries
header schema. Fields are added incrementally later.

## Schema ownership

Each source owns exactly one ClickHouse table schema, 1:1:

```
Source: filebeat
  └── Schema: logs_beats_filebeat
        ├── meta_schema.yaml       (base columns, types, use cases)
        ├── derived_schema.yaml    (source-specific overrides)
        └── additional_fields.yaml (enrichment fields)
```

The type registry (`type_registry.yaml`) maps primitives to ClickHouse types
and is global, not per-source. The schema section feeds the `SchemaBuilder` /
`ClickHouseSchema` pipeline; what the Source changed is that create, migrate
and drop are triggered from the source's state rather than from a standalone
schema config.

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
  - name: message
    type: text
    use_case: fulltext
```

Loader directives in `comment` survive into the ClickHouse column comment. The
full type system, the primitive-to-ClickHouse mapping and the use cases are in
[schema.md](schema.md).

| Source event | Schema action |
|-------------|---------------|
| created / active | `CREATE TABLE IF NOT EXISTS` (idempotent) |
| schema YAML updated | `ALTER TABLE` via SchemaModifier (add/modify columns) |
| dormant | none -- the table is retained, pre-positioned for reactivation |
| disabled | guarded reclaim -- the table is dropped behind the caller's guard |
| deleted | the table is retained; drop it by hand if the data is not wanted |

Migrating off Elastic starts from the index template rather than from nothing:
the converter maps Elastic types to primitives and analyzers to use cases and
emits the schema YAML, which the source then names. The mapping table is in
[schema.md](schema.md).

## What a write is validated against

| What | Rule |
|---|---|
| `_source` label | unique, and `[a-z]([a-z0-9-]*[a-z0-9])?` up to 40 chars |
| match rule | no conflict across non-disabled sources (same field+operator+value); a dormant source HOLDS its match, only disabling releases it |
| match operator | receiver-evaluable (`equals` or `exists`); `always` only on the reserved `main` source |
| fetcher route | must not name its own source |
| flow | runnable here: the deployment offers the transport, the transform app carries it, archive only on the bus |
| transform | engine must be one the app manifest catalogues (`vrl`, `vector`, `elastic`) |
| catalogue entry | the intake must be one the entry arrives by, the transform one it ships, and the source name a legal label - the entry's own name is used when it is one |
| schema | files exist and pass SchemaBuilder validation, and carry `_source` as a column (injected if missing) |
