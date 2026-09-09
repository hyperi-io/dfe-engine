<!--
  Project:   dfe-engine
  File:      docs/data-plane/source-routing.md
  Purpose:   The exact receiver and loader config a source definition compiles into
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# What a source compiles into

The engine emits the receiver's and the loader's own native config -- not a DFE
wrapper the services then translate. This is what to read when a deployed pod's
behaviour has to be traced back to a source definition. The shape of the flow
is [source-flow.md](source-flow.md); the fields are
[source-definition.md](source-definition.md).

## The receiver

`compile_receiver_routing` emits the exact serde the receiver deserialises
(`SourceRule` / `RoutingConfig` in the receiver's `src/config/mod.rs`):

1. Iterate ACTIVE sources -- dormant and disabled are excluded
2. Each `match` becomes one `source_rules` entry, first match wins
3. A matching rule stamps `_source` into the JSON payload
4. Topic = `source_to_topic[_source]` when overridden, else `{_source}{topic_suffix}`
5. An unmatched record gets `default_source`

```yaml
routing:
  source_rules:                   # Compiled from Source.match, first match wins
    - field: tags.collector.type  # Dotted path into the incoming payload
      mode: key_value_set         # key_present | key_value_set | key_value_use
      match_value: filebeat       # key_value_set only
      source: filebeat            # _source to stamp
  default_source: main            # _source when no rule matches
  topic_suffix: _land             # Topic derives as {_source}{topic_suffix}
  source_to_topic: {}             # Per-source topic overrides (rarely needed)
```

| Source `match.operator` | Receiver `mode` |
|-------------------------|-----------------|
| `equals` | `key_value_set` |
| `exists` | `key_present` |
| `always` | no rule; `default_source` already sends an unmatched record there |

The other four operators (`not_equals`, `includes`, `starts_with`, `ends_with`)
have no receiver mode -- a documented receiver gap. The registry rejects them at
save time for any non-disabled source; a legacy stored doc that still carries
one is skipped at compile with a loud warning rather than failing the whole
receiver config.

On the direct transport there is no topic between stages, so the same match
compiles a second time into `destinations`: a rule carrying that field and value
sends the record to a named endpoint -- the source's transform instance when it
has one, else the loader -- and every name it uses is listed beside the rules as
`<name>: {grpc: {endpoint: <uri>}}`. A match that tests no value (`exists`)
cannot name a destination, so a direct source with a transform is refused at
save.

## The loader

The loader takes the target table from the first `table_fields` hit, and its
default is `_source` -- the field the receiver just stamped. The two halves meet
with no lookup at all:

1. The loader reads `_source` from the payload
2. `_source` is the target table: `{default_db}.{_source}`
3. `source_to_table` carries only the sources whose table name differs
4. The schema for that table comes from the source definition

```yaml
# The compiled loader block (dfe-loader RoutingConfig):
routing:
  table_fields: [_source]         # The field the receiver stamped
  default_db: dfe                 # Database (or per-org via db_fields + org_routes)
  default_table: main             # Where a record with no _source lands
  source_to_table: {}             # ACTIVE sources whose table name differs
```

Every key the engine emits must exist in dfe-loader's `RoutingConfig`
(`src/config/pipeline.rs`): serde drops an unknown key without complaint, so an
invented one would silently leave the loader on its own default. The engine
model is pinned to that struct by a test that reads the Rust source.
