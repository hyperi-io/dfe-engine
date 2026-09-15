# The topic contract

A defined source implies Kafka topics: `<source>_land` always, and `<source>_load`
when the source has a transform. Those topics are the engine's for the source's
whole life, so nothing here depends on the broker's `auto.create.topics.enable` -
which creates a mis-partitioned, unmanaged topic on first produce, and is
unavailable on Confluent Cloud's non-Dedicated tiers anyway.

The deploy creates a source's topics and the delete removes them. Everything else
is `/api/v1/kafka/topics`.

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
| `kafka.topic_replication_factor` | Replication factor for topics DFE creates. A converge reports a difference and changes nothing. |
| `kafka.topic_retention_ms` | `retention.ms` on the topics DFE creates and converges. Unset leaves the broker default. |
| `kafka.topic_cleanup_policy` | `cleanup.policy`, same. |
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
`redpanda-no-tls`, not `strimzi` with something turned down - see
[../deployment/managed-kafka-lifecycle.md](../deployment/managed-kafka-lifecycle.md).

Neither a deploy nor a delete ever fails on the topic step: the schema is already
live by the time the topics are ensured, and the source is already gone by the
time they are removed, so an unreachable broker is reported on the response
instead.
