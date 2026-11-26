<!--
  Project:   dfe-engine
  File:      docs/data-plane/source-routing.md
  Purpose:   The exact receiver and loader config a source definition compiles into
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# What a source compiles into

The engine emits the receiver's and the loader's native config -- not a DFE
wrapper the services then translate. This is what to read when a deployed pod's
behaviour has to be traced back to a source definition. The flow
is [source-flow.md](source-flow.md). The fields are
[source-definition.md](source-definition.md).

## The receiver

The engine emits four receiver blocks, plus an `auth` block per bearer listener, each the exact serde the receiver deserialises (`src/config/mod.rs`, and scalo-rs `rules::RuleSetConfig` for `rules`):

1. Iterate ACTIVE sources -- dormant and disabled are excluded
2. Each source's `match` or `when` becomes one block of `rules` rows, and each bound compatibility profile one block per field it sets. Blocks run in the deployment's rule order, first match per field wins
3. A matching row sets `_source` in the JSON payload. Only a bound profile's rows set `_org_id`, otherwise a record's org is its ingest credential's binding, else `routing.default_org_id`
4. Topic = `source_to_topic[_source]` when overridden, else `{_source}{topic_suffix}`
5. An unmatched record gets `default_source`

```yaml
routing:
  default_source: main            # _source when no rule matches
  default_org_id: acme-prod       # _org_id an unbound sender's records take: your org's first tenant id
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
server:                           # Likewise grpc, otlp, splunk_hec and prometheus_rw
  auth:
    mode: bearer                  # none while no accepted credential lists the listener
    bearer:
      secret_source: "aws:<DFE_ENGINE_SECRETS_NAME_PREFIX>/ingest/bearer-tokens/http"
      refresh_interval_secs: 60
```

| Source `match.operator` | Receiver row `op` |
|-------------------------|-------------------|
| `equals` | `equals` |
| `not_equals` | `equals` with `negate: true` |
| `exists` | `exists` |
| `includes` | `contains` |
| `starts_with` | `starts_with` |
| `ends_with` | `ends_with` |
| `always` | no row, because `default_source` already sends an unmatched record there |

A match is one test, or `all` or `any` of several, nested up to four groups deep, and `negate` inverts the test or group it sits on. The receiver has `all` but no `any`, so the engine compiles a match to rows, any one of which matching labels the record:

| Source match | Receiver rows |
|---|---|
| a test | one row, `source.<name>` |
| `all: [a, b]` | one row, `match: {all: [a, b]}` |
| `any: [a, b]` | one row per branch, `source.<name>.1` and `.2`, in declared order |
| `negate` on `all` | one row, `match: {all: [...], negate: true}` |
| `negate` on `any` | one row of the negated parts, by de Morgan's laws |
| `all: [a, any: [b, c]]` | `all: [a, b]` and `all: [a, c]` |

A match that compiles to more than 32 rows is refused at save. Move the `any` outward. A source may carry `when`, a [CEL](https://github.com/cel-expr/cel-spec/blob/master/doc/langdef.md) Fast condition, in place of `match`. It compiles to one row with that `when`, and the save checks it under the profile the receiver compiles with, so regex, iteration and time functions are refused with the span of the call.

Every save compiles the source's rows and checks them with scalo.rules as the receiver will. A refusal is a 422 `invalid_rule`, each problem with its field and, for `when`, its UTF-16 span in `rule_errors`. A stored block that no longer compiles is left out of the rule set with a loud warning rather than failing the whole receiver config, and `GET /api/v1/ingest/rules` lists it with `compiled: false`.

## Rule order and compatibility profiles

Blocks run in the order `governance/settings/ingest-order.yaml` stores, one list per field. A block the order does not name runs after every one it does, sources by name, then profiles by id. The receiver's `default` runs last for each field and does not move. `GET /api/v1/ingest/rules` lists every block in the order it runs, with each one's warnings, among them the rows an earlier row always shadows. `POST /api/v1/ingest/rules/order` takes one field's full list with that list's `If-Match`.

| Answer | When |
|---|---|
| 200 | stored, and the receiver rules recompiled. `reload: hot` is the receiver taking it where it stands |
| 409 `order_conflict` | the list names other blocks than the field has now, the current list in `blocks` |
| 412 `etag_mismatch` | the order changed since it was read |
| 422 `invalid_order` | a block listed twice, or the pinned `default` |
| 428 | no `If-Match` |

A compatibility profile is versioned data in the dfe-schemas wheel, copied into the deploy repo's `config/compat-profiles/` at startup, never code. Its rows read another platform's header and set `_source`, `_org_id` or both. A source binds profiles with `compat_profiles`, else takes the deployment's list (`PUT /api/v1/ingest/compat`), and `[]` binds none.

- A row setting `_source` runs once for the deployment, with its `aliases` lookup filled from each bound source's `aliases.<profile>`, mapped to that source's label. A value no source aliases sets nothing.
- A row setting `_org_id` compiles once per bound source, guarded with `_source equals <label>`, so it reads that source's records and no others. The receiver sets `_source` first in the same pass.
- A row the profile lists under `opt_in` compiles just for a binding that names it.
- One version of a profile compiles per deployment. A save binding a second is refused.

`elastic-agent` routes on `data_stream.dataset`, Elastic's identifier per source. `dfe-legacy-2x` reads a DFE 2.0 or 2.1 sender's `tags.event.category` and `tags.event.org_id`.

## Ingest credentials and bearer listeners

`ingest_bindings` holds each ingest credential's orgs as the tenant ids they own, and its default as its default org's first tenant id. Every record's `_org_id` is held to the binding of the credential that sent it, and a credential with no entry may land records under `default_org_id` alone. A credential naming an org that is no longer registered is left out for that reason.

Each bearer listener runs `mode: bearer` while a credential whose owning org is registered and enabled lists it, other than the engine's own `dfe-engine-synthetic-data` (see `docs/control-plane/synthetic-data.md`), reading the `<id>:<token>` lines the engine writes to `ingest/bearer-tokens/<listener>`. With none it runs `mode: none`, and every record it takes lands under `default_org_id`. `secret_source` follows `DFE_ENGINE_SECRETS_PROVIDER`: `vault:<mount>/...:value` for openbao, `aws:<DFE_ENGINE_SECRETS_NAME_PREFIX>/...` for aws, and `file:/var/run/secrets/dfe/ingest-bearer-tokens/<listener>` for file, where the deployment mounts that one directory read-only into the receiver. On Kubernetes that directory is the Secret `DFE_ENGINE_INGEST_BEARER_SECRET` names, which the engine writes one key per listener into. A credential on any other backend fails the compile, naming the setting to change. The receiver re-reads each listener's secret every 60 seconds (`refresh_interval_secs`), so a deleted, disabled or rotated credential's token stops within about a minute, plus the kubelet's sync of a mounted Secret.

On the direct transport there is no topic between stages, so `destinations.sources` keys each source's landing label to a named endpoint -- the loader, because a source with a transform is refused on direct -- and every name it uses is listed beside it as `<name>: {grpc: {endpoint: <uri>}}`. The key is the `_source` the row set, so the one rule evaluation that labels a record also picks where it goes.

## The loader

The loader takes the target table from the first `table_fields` hit, and its
default is `_source` -- the field the receiver just stamped. The two halves meet
with no lookup at all:

1. The loader reads `_source` from the payload
2. `_source` is the target table: `{default_db}.{_source}`
3. `source_to_table` maps every ACTIVE source to its table
4. The table's columns come from ClickHouse `system.columns`, which the engine created from the source definition ([schema-sync.md](schema-sync.md))
5. `table_schema_versions` maps each deployed source's table to `<deployed version>+<meta-schema version>+<derived-schema version>`, so a deploy that adds columns changes the block and the loader re-reads that table. The last two parts are the versions the table was built from: the source's pin, else the schema's `current`, so a new version changes the string for a source that pins none. A part is left out when the source has no such schema
6. `table_schemas` lists each deployed source's table columns as its DDL created them: `name` and `type`, plus `default_kind` (`MATERIALIZED`, `ALIAS` or `DEFAULT`) and `comment` where the column has one. The engine builds them from the same schema files as the DDL, with no ClickHouse read, so the loader takes a newly deployed table's first records without waiting on `system.columns`. ClickHouse's own read still replaces them

```yaml
# The compiled loader block (dfe-loader RoutingConfig):
routing:
  default_db: dfe                 # Database (or per-org via org_routes)
  default_org_id: acme-prod       # Tenant for a record with no _org_id
  source_to_table: {}             # Every ACTIVE source: its table
  table_schema_versions: {}       # Each deployed source's table: <deployed version>+<meta version>+<derived version>
  table_schemas: {}               # Each deployed source's table: [{name, type, default_kind, comment}]
```

A fetched source's fetcher is compiled with `org_id`, the first tenant id of the org its `fetcher.org` names (`default_org_id` when unset), and `default_org_id`.

`default_org_id` is the same in every app: your org's first tenant id, your org being the one `PUT /api/v1/system/organisation` names, so a hunt for your org reads every record that names no org. Naming your org, or changing its org IDs, recompiles the apps. With no org named as yours, or one that is not registered and enabled, it is `unassigned`, which no org may hold as a name or an org ID, so only a caller reading every org reads those records. With gitops off the engine writes no app overlay, so the apps keep their built-in `default`.

Every key the engine emits must exist in dfe-loader's `RoutingConfig`
(`src/config/pipeline.rs`): serde drops an unknown key without complaint, so an
invented one would silently leave the loader on its default. The engine
model is pinned to that struct by a test that reads the Rust source.
