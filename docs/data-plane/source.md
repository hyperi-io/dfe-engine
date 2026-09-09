# Source - the top-level data abstraction

**Status:** Shipped (`SourceRegistry` + the Source model are the live path).

A Source is one distinct stream of events entering the platform -- `filebeat`,
`syslog`, `crowdstrike-edr`, `windows-audit`. It is the only entity the API,
the CLI and the console CRUD, and everything a stream needs hangs off it:
where records come in, whether they are transformed, the table they land in,
the detections that run against it.

Without it, onboarding a stream means editing four systems that share no
concept of "this is one source" -- receiver match rules, a transform's own
input and output topics, loader table routing, and a table definition with no
link back to any of them -- and hoping the naming stays consistent. It never
did.

## Two tiers, not two systems

**Global services** are infrastructure, configured once and shared by every
source. Their config says *how*: brokers, connection strings, TLS, buffer
sizes, scaling.

| Service | Role |
|---------|------|
| **receiver** | HTTP/gRPC ingestion, match routing, `_source` injection |
| **loader** | landing records to ClickHouse, schema-aware insert |
| **archiver** | landing records to S3 or a file archive |

**Sources** say *what*. Each one contains its own scoped components rather
than being listed inside a service's config:

| Component | Required | Purpose |
|-----------|----------|---------|
| **identity** | Yes | `_source` label, display name |
| **origin** | Yes | Exactly one of a receiver `match` rule or a `fetcher` |
| **schema** | Yes | ClickHouse table definition (starts as the common header alone). See [schema.md](schema.md) |
| **transform** | No | Enrichment or normalisation stage |
| **flow** | No | `transport` (bus or direct) and `archive`; unset takes the deployment default. See [source-flow.md](source-flow.md) |
| **rules** | No | SQL detections against this source's table |
| **views** | No | Naming-standard views (sigma, ecs, cim, ocsf) with per-source overrides |

A source-scoped component is logical config, not a deployment the operator
manages. One transform-vector deployment may run the transforms of twenty
sources; the source says *this source needs a vector transform with this
config*, and the deployment layer decides which instance runs it.

| Source component | Runs on | How |
|-----------------|---------|-----|
| `source.match` | the receiver pool | compiled into receiver `source_rules` |
| `source.fetcher` | one fetcher instance per source | the stanza is compiled into its overlay |
| `source.transform` | one transform instance per source | the stanza is compiled into its overlay |
| `source.schema` | dfe-engine | DDL generated and applied by the engine |
| `source.rules` | the hunt runner | SQL on a schedule, matches to alerts |
| `source.views` | dfe-engine | naming-standard compatibility views |

## The `_source` label

`_source` is the single identifier that ties the four systems together. The
receiver stamps it into the JSON payload at ingest, and from there every other
name is derived rather than configured:

```
_source = "filebeat"

Kafka topics:
  filebeat_land     raw records from the receiver
  filebeat_load     transformed records (only when a transform exists)

ClickHouse:
  Table: {db}.filebeat
  _source column value: "filebeat"
```

Because it is stamped before the record reaches the bus, the downstream stages
read it directly and carry no routing config of their own.

### Naming rules

- A Kubernetes DNS-1123 label starting with a letter: `[a-z]([a-z0-9-]*[a-z0-9])?`
- Hyphens, never underscores. A source-bound app deploys one instance named for
  the source, and that name becomes an Argo Application and a set of Kubernetes
  object names, so the charset has to be a subset of what a label allows.
- Max 40 characters, the cap the instance name carries
- Unique across all sources

A hyphenated name has to be quoted in a ClickHouse identifier position. The DDL
generator does not quote it yet, so a hyphenated source cannot have its table
DDL generated -- see `schema/schema_ddl.py`.

## Where to read next

| Doc | Covers |
|---|---|
| [source-definition.md](source-definition.md) | the source YAML, field by field, and what a write is validated against |
| [source-registry.md](source-registry.md) | states, storage backends, CRUD, topics on deploy, the API surface |
| [source-flow.md](source-flow.md) | the two transports, what the engine compiles, how a change reaches a pod |
| [source-routing.md](source-routing.md) | the exact receiver and loader config a source compiles into |
| [source-detections.md](source-detections.md) | rules, hunts, and Sigma views |
