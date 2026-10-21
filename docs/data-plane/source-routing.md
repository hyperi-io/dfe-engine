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

The engine emits four receiver blocks, each the exact serde the receiver deserialises (`src/config/mod.rs`, and scalo-rs `rules::RuleSetConfig` for `rules`):

1. Iterate ACTIVE sources -- dormant and disabled are excluded
2. Each `match` becomes one `rules` row, `source.<name>`, first match wins
3. A matching row sets `_source` in the JSON payload. No row sets `_org_id`: a record's org is its ingest credential's binding, else `routing.default_org_id`
4. Topic = `source_to_topic[_source]` when overridden, else `{_source}{topic_suffix}`
5. An unmatched record gets `default_source`

```yaml
routing:
  default_source: main            # _source when no rule matches
  default_org_id: default         # _org_id an unbound sender's records take
  topic_suffix: _land             # Topic derives as {_source}{topic_suffix}
  source_to_topic: {}             # Per-source topic overrides (rarely needed)
  source_org_id: disallow         # ingest.source_org_id: whether a sender's _org_id is kept
rules:
  targets: [_source, _org_id]     # The order the receiver sets them in
  rules:                          # Compiled from Source.match, first match wins
    - id: source.filebeat
      match: {field: tags.collector.type, op: equals, value: filebeat}
      set: {_source: {value: filebeat}}
ingest_bindings:                  # Compiled from the ingest credentials, as tenant ids
  credentials:
    acme-agents: {orgs: [acme-prod, acme-dev], default_org: acme-prod}
```

| Source `match.operator` | Receiver row `op` |
|-------------------------|-------------------|
| `equals` | `equals` |
| `exists` | `exists` |
| `always` | no row, because `default_source` already sends an unmatched record there |

Four operators (`not_equals`, `includes`, `starts_with`, `ends_with`) compile to no row. Saving a non-disabled source that uses one is refused, and a stored doc that still carries one is skipped at compile with a loud warning rather than failing the whole receiver config.

`ingest_bindings` holds each ingest credential's orgs as the tenant ids they own, and its default as its default org's first tenant id. Every record's `_org_id` is held to the binding of the credential that sent it, and a credential with no entry may land records in the default org alone. A credential naming an org that is no longer registered is left out for that reason.

On the direct transport there is no topic between stages, so `destinations.sources` keys each source's landing label to a named endpoint -- the source's transform instance when it has one, else the loader -- and every name it uses is listed beside it as `<name>: {grpc: {endpoint: <uri>}}`. The key is the `_source` the row set, so the one rule evaluation that labels a record also picks where it goes.

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
  default_db: dfe                 # Database (or per-org via org_routes)
  default_org_id: default         # Tenant for a record with no _org_id
  source_to_table: {}             # ACTIVE sources whose table name differs
```

A fetched source's fetcher is compiled with `org_id`, the first tenant id of the org its `fetcher.org` names (the default org when unset), and `default_org_id`.

Every key the engine emits must exist in dfe-loader's `RoutingConfig`
(`src/config/pipeline.rs`): serde drops an unknown key without complaint, so an
invented one would silently leave the loader on its own default. The engine
model is pinned to that struct by a test that reads the Rust source.
