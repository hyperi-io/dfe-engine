# Schema bootstrap -- the engine is the only controller

dfe-engine applies every ClickHouse object and every bootstrap Kafka topic a DFE
deployment needs, at its own startup, from the pinned `dfe-schemas` wheel. No
other component creates, alters or drops a database, table, view, role or topic:
not a Job, not an init container, not an app at runtime, not the OTel collector.

`dfe-schemas` is where those objects are DEFINED. `manifest.yaml` lists every one
of them in dependency order, and the engine iterates that list and nothing else.
Adding a table is a manifest edit plus a definition file; it is never an engine
release.

## What runs, in what order

The phase sits inside the API lifespan, ahead of readiness
(`src/dfe_engine/schema/phase.py`):

1. **Connect**, retrying an unreachable ClickHouse for
   `DFE_CLICKHOUSE_BOOTSTRAP_WAIT_SECONDS` (default 180). The datastore can be in
   the same deploy wave, so not-up-yet and broken are different answers.
2. **Resolve the schema tree**: the wheel inside the image, read-only, then the
   deployment's optional overlay directory (`DFE_SCHEMAS_OVERLAY_DIR`).
3. **Render the plan** for the topology sensed off the live server -- plain
   `MergeTree` on a standalone node, `ReplicatedMergeTree` against a Replicated
   database, `ReplicatedMergeTree ON CLUSTER` against an Atomic database on a real
   cluster. Every rendered object carries a checksum taken with the topology token
   removed, so one schema reads as one checksum on either shape.
4. **Take the lease** in `schema_lock`. A second replica waits, then reports what
   the holder left without applying. An expired lease is taken over.
5. **Compare** against `schema_migrations` and against the live catalogue, and
   apply what is additive.
6. **Record** every applied object: its kind, the dfe-schemas release, the engine
   release, the checksum, the statement and the action.
7. **Create the topics**, create-only, skipping with a logged notice where the
   deployment has no bus.
8. **Release the lease** and report.

`schema_migrations` and `schema_lock` are manifest objects like any other and the
manifest declares them first, so the same pass creates them before it records
anything into them. The engine carries no DDL even for its own bookkeeping.

## What is applied, and what is refused

Applied automatically:

- an absent object is created
- a column the definition adds and the table lacks is added
- a plain view whose SELECT changed is replaced, because it holds no state
- a role, settings profile, quota or grant is re-asserted, because every statement
  in that set converges

Refused and named on the status route:

- a changed column type or codec, an `ORDER BY`, a `PARTITION BY` or a TTL change
- a changed materialised view, because it holds what it has already aggregated
- a live column the definition no longer declares, which is reported and never
  actioned

A refusal is not a failed pass: the object is named, the rest of the manifest
still converges, and readiness is still reported. An operator applies the change
deliberately with `dfe schema apply --allow-drift`, which carries out the two
ClickHouse has an operation for -- a column type and a TTL. An `ORDER BY` or
`PARTITION BY` change stays refused either way, because the table has to be
rebuilt.

## Readiness

`/readyz` gains a `schema` check beside the ClickHouse ping. Ready means the last
pass converged, or the phase is switched off and gates nothing. A FAILED pass
leaves the pod UP and NotReady with the cause on `GET /api/v1/system/schema`, so
an operator reads it off the API rather than off a crash loop. `/livez` is never
consulted -- the pod is not restarted out from under whoever is reading it.

## The overlay

A deployment's own schema tree is additive. It may declare objects the core does
not; an object of its own whose definition sits under a core directory
(`tables/core/`, `tables/internal/`, `tables/otel/`, `tables/meta/`,
`common-header/`, `registries/`, `roles/`, `topics/`, `views/`) is refused by name
and reported, and the core apply is unaffected. The overlay is optional: an
install with no deploy repo boots core-only.

## Dials

| Variable | Default | What it does |
|---|---|---|
| `DFE_CLICKHOUSE_BOOTSTRAP_TABLES` | `true` | Apply the manifest at startup. Off reports the schema state as unknown and gates nothing. |
| `DFE_CLICKHOUSE_BOOTSTRAP_WAIT_SECONDS` | `180` | How long to keep retrying an unreachable ClickHouse before reporting the pass failed. |
| `DFE_KAFKA_BOOTSTRAP_TOPICS` | `true` | Create the declared topic set at startup, create-only. |
| `DFE_KAFKA_TIERED_STORAGE` | `false` | The brokers tier to object storage, so the landing topic is created with `remote.storage.enable`. Off leaves the key unset rather than false, which is what a broker-level setting needs. |
| `DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES` | the manifest's | `max.message.bytes` on every topic the engine creates, this set and each source's `_land` and `_load`. Set it to the deployment's message size: a managed broker capped below the manifest's refuses the create, and the dead-letter set then holds the engine NotReady. |
| `DFE_SCHEMAS_OVERLAY_DIR` | unset | The deployment's additive overlay tree. |

The two bootstrap dials are separate rather than one: they fail for different
reasons, and a deployment with an external broker wants one off and the other on.

A stack that writes to ClickHouse with the engine switched off is unsupported. No
engine, no tables.

## Operator surface

- `GET /api/v1/system/schema` -- the last pass, object by object: state, versions,
  topology, per-object action and checksum, the refused list, the overlay
  refusals, the topics created
- `GET /api/v1/system/version` -- carries the dfe-schemas release as `schemas`
- `dfe schema plan` -- what an apply would do, changing nothing
- `dfe schema apply [--allow-drift]` -- the boot phase by hand, under the same
  lease
- `dfe schema status` -- the ledger: which release each object on this cluster
  came from
- `dfe_schema_bootstrap_state` (0 unknown, 1 converged, 2 failed, 3 running,
  4 observed), `dfe_schema_bootstrap_duration_seconds`,
  `dfe_schema_version_info`, `dfe_schema_objects_refused`

## What the engine still renders itself

Per-SOURCE objects are defined at use time from an operator's own config, so they
are not manifest data:

- a source's table and its standard views, rendered by
  `src/dfe_engine/schema/schema_builder_v2.py` and applied through the same
  sensing resolver
- an operator's own quota tiers and the per-org and per-group ClickHouse users,
  reconciled by `src/dfe_engine/governance/ch/`

Both go through `SchemaApplier`, so the topology cannot be bypassed. A guard test
(`tests/unit/test_schema/test_schema_control_rules.py`) fails the build on a
`CREATE TABLE`, `CREATE DATABASE` or `CREATE MATERIALIZED VIEW` anywhere under
`src/` outside `dfe_engine/schema/`, and on a literal that qualifies an object the
manifest does not declare.
