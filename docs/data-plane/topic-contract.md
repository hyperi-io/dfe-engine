# The topic contract

A defined source implies Kafka topics: `<source>_land` unless it lands on the shared topic, and `<source>_load`
when the source has a transform. Those topics are the engine's for the source's
whole life, so nothing here depends on the broker's `auto.create.topics.enable` -
which creates a mis-partitioned, unmanaged topic on first produce, and is
unavailable on Confluent Cloud's non-Dedicated tiers anyway.

The deploy creates a source's topics and the delete removes them. The old `_load` topic of a version that dropped its transform goes once its transform instance has left the deploy repo, the transform's consumer group has no live member, and no consumer group has records left to read on it. The apps reconcile after a later source write, `POST /api/v1/sources/reconcile-apps`, or the engine's next start deletes it. A transform with no consumer group the engine can name keeps the topic. Everything else
is `/api/v1/kafka/topics`.

A fetcher source pushing to the main topic is the exception at both ends: it arrives on the landing source's `main_land`, so it creates no landing topic on deploy and removes none on delete. Deleting it otherwise takes `main_land` out from under the receiver's default flow and every other source landing there.

## Deleting a stranded `_load` topic

Those checks stop the delete while a writer runs, but not when one starts again in the sub-second gap between the backlog read and the delete. With the broker's `auto.create.topics.enable` off, its records are refused. With it on, its first produce re-creates `_load` at broker defaults, which suppresses `_land` again and strands what lands there until retention. dfe-infra's cluster profile turns auto-create off, while its single profile and dfe-docker's default turn it on.

Once `_load` is gone the loader reads `_land` from `earliest`, the scalo-rs consumer default, so the source's retained `_land` records load a second time. Those are duplicates, which the at-least-once contract allows, and never loss.

## The surface

| Route | What it does |
|---|---|
| `GET /api/v1/kafka/topics` | Per-topic existence, partitions, replication factor, config and drift against what the source asks for. Reads only. |
| `POST /api/v1/kafka/topics/ensure` | Creates what is missing. Idempotent, and it never reshapes a topic that already exists. |
| `POST /api/v1/kafka/topics/update` | Converges an existing topic: an incremental alter of the configs the spec names, and a partition increase. |
| `POST /api/v1/kafka/topics/remove` | Deletes one source's topics. Needs `confirm=true`; the records on them go with them. |

Each takes `?source=` to narrow to one source and `?dry_run=true` to report without
changing anything. The topic names are never typed by the caller - the request
names a source and the engine derives which topics follow, which is what makes
this a governed surface rather than a broker admin shell.

RBAC follows the source scopes: `source:read` to look, `source:deploy` to ensure
or converge, `source:delete` to remove. The right to deploy a source is the right
to make its topics exist. The CLI is generated from the same spec, so
`dfe kafka topics list|ensure|update|remove` is the same surface over HTTP.

A deployment that runs no bus answers 503 rather than an empty list, so "no topics
needed" and "not this deployment's job" are never confused.

## What a converge will not do

| Refused | Why |
|---|---|
| Partition decrease | Kafka has no such operation, and the records already assigned to the partitions it would drop have nowhere to go. |
| Replication-factor change | That is a partition reassignment, not a topic alter. |
| Creating a topic that does not exist | That is `ensure`. Doing it here would hide a source that never deployed. |

A refusal is reported apart from a broker failure, because an operator fixes the
two in different places.

## The dials

| Setting | Effect |
|---|---|
| `kafka.topic_partitions` | Partition count for topics DFE creates. A converge widens an existing topic to it. |
| `kafka.topic_replication_factor` | Replication factor for topics DFE creates. Unset takes the dfe-schemas landing default (3) clamped to the broker count the bus reports, so a managed broker that refuses fewer than 3 replicas gets 3. A converge reports a difference and changes nothing. |
| `kafka.topic_retention_ms` | `retention.ms` on the topics DFE creates and converges. Unset leaves the broker default. |
| `kafka.topic_cleanup_policy` | `cleanup.policy`, same. |
| `kafka.topic_compression_type` | `compression.type` on the topics DFE creates and converges. Default `producer`: the broker stores each batch in the codec its producer chose (zstd level 3 across DFE) and never recompresses it. Empty leaves the broker default. |
| `kafka.ensure_topics` | Whether the topics are DFE's to manage. Unset follows `transport.bus_present`. |

Only the configs the spec names are ever compared, so a config an operator set
outside it is not drift and a converge never proposes to undo it.

`kafka.ensure_topics` set true also runs one ensure pass over every source at
startup, so a deployment restored from its config repo has its topics before
anything produces into them. Left unset it does not: a deployment that merely
carries a bus may not have been told where the broker is, and every boot would
spend the admin timeout finding that out.

## Reaching the broker

`kafka.provider` derives `security.protocol` and `sasl.mechanism`; the mechanism
is never hand-set. One key names one LISTENER, so a DFE-owned broker on the
TLS-off listener the deploy charts stand up is `strimzi-no-tls` or
`redpanda-no-tls`, not `strimzi` with something turned down. SCRAM is a
challenge-response, so the password never reaches the wire on that listener; no
PLAIN or OAUTHBEARER provider has a TLS-off key.

A delete never fails on the topic step: the source is already gone by the time its topics are removed, so an unreachable broker is reported on the response instead. A deploy of a source on the bus does fail on it, with 502 `topics_not_created` for a topic the broker did not create. An old `_load` topic never fails it: the deploy reports one it could not delete yet in `topics_pending_removal`.
