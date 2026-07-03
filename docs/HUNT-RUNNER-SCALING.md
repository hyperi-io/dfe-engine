<!--
  Project:   dfe-engine
  File:      docs/HUNT-RUNNER-SCALING.md
  Purpose:   How the pull-based hunt runner scales out and to zero (KEDA)
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Hunt-runner scaling: deterministic-due, scale-to-zero, no engine dependency

The hunt runner is pull-based and multi-pod (see auto-memory
`project_hunt_runner_architecture`). This doc is the SCALING contract: how KEDA
wakes the first worker from zero, how it scales out, how it scales back to zero,
and the property that matters most - the runner has NO operational dependency on
dfe-engine.

## The one hard requirement: due-detection with zero workers

KEDA can only wake the first worker if "is any hunt due right now?" is answerable
with no worker running - i.e. as a query KEDA itself runs against ClickHouse. But
the per-hunt schedule (interval + stable phase offset) otherwise lives only in a
running worker's memory (loaded from the gitops hunt configs). At zero workers
there is nobody to ask.

So the schedule is MATERIALISED into a small ClickHouse table, `hunt_schedule`, at
config-deploy time. The scaler then reads it directly. Three tables already exist
for coordination (`ch_coordinator.py`): `hunt_lease`, `hunt_watermark`,
`hunt_state`. `hunt_schedule` is the fourth, and the only one written outside the
worker loop.

```
hunt_schedule(hunt_id, interval_seconds, phase_offset, enabled, updated)
  ReplacingMergeTree(updated) ORDER BY hunt_id
```

`enabled` is a soft tombstone: a deleted hunt is written `enabled=0` so it stops
waking KEDA, without a DELETE.

## The scaler query (deterministic-due)

KEDA runs `schedule.due_query(db)` against ClickHouse (via the MySQL scaler - see
"KEDA ScaledObject" below) - the count of hunts that are due-and-unclaimed. It uses
the EXACT arithmetic the worker uses (`spread.current_fire`):

```
boundary = intDiv(now(), interval) * interval      -- start of this interval
fire     = boundary + phase_offset                  -- this interval's scheduled fire
due      = now() >= fire                             -- the fire has arrived
           AND watermark < fire                      -- that fire not already completed
           AND lease_until <= now()                  -- not currently running
```

`due_count > 0` -> scale up; `== 0` -> scale to zero. The `enabled=1` filter is
applied in an inner subquery so a tombstone (interval=0) can never reach the
`intDiv` (no divide-by-zero).

### Why the watermark comparison is exact

The worker writes `watermark = scheduled_start` (the fire time) after a run commits
(`worker.py`: `window(...)` returns `end = scheduled_start`, then
`set_watermark(end)`). So the watermark is in the SAME epoch-fire scale as the
boundary arithmetic - `watermark < fire` means precisely "this fire has not been
completed". No unit mismatch, no data-time-vs-fire-time skew.

## Why it is RELIABLE (the property the decision hinged on)

Scale-to-zero is only safe if KEDA's view of "due" never diverges from a worker's.
It cannot, because both compute the same thing from the same inputs:

1. **Parity, proven.** `tests/hunt_runner/test_schedule.py` asserts the scaler
   predicate equals `runner.tick`'s own gate (via `spread.current_fire`/`due_now` +
   the watermark + lease checks) across a full matrix of (hunt_id, interval, now,
   watermark, lease). `tests/integration/test_hunt_schedule_scaler.py` then proves
   the SQL string implements that predicate against real ClickHouse - counting
   exactly the runnable hunts, excluding tombstoned / future-watermark / leased
   hunts, and returning 0 when every hunt is caught up.
2. **Single source of truth for the offset.** The materialiser computes
   `phase_offset` with the same `spread.phase_offset` the worker uses, so the two
   can never drift. Change the offset algorithm once and both move together.
3. **Gate on the signal, not a timer.** KEDA polls the backlog query (a real
   readiness signal), it does not race a clock. Worst-case wake latency is
   `pollingInterval + pod-start`; for hunts (minute-plus intervals) that is
   immaterial. This is the "gate on the real condition" rule from the testing
   standard applied to autoscaling.
4. **Self-healing.** If a worker claims a hunt then dies before writing the
   watermark, its lease expires (`lease_seconds`, default 300) and the fire is
   still owed (`watermark < fire`), so the scaler keeps >=1 worker and the hunt is
   reclaimed. Nothing is lost; nothing double-runs (the lease gives mutual
   exclusion, and a rare settle-window race is absorbed by the idempotent windowed
   INSERT).
5. **No thundering herd.** The phase offset spreads same-interval hunts across the
   interval, so `due_count` rises gradually and KEDA scales out smoothly rather
   than all-at-once at the boundary.

Bounded catch-up: a worker that starts inside `[boundary, boundary+offset)` waits
for this interval's fire rather than back-filling a still-owed prior fire; the
scaler matches it exactly (no busy-spin), and the owed fire is picked up at the
next `due_now`. Max lateness for any fire is under one interval - inherent to the
phase-offset design, and identical whether the decision is made by KEDA or a
resident worker.

## No operational dependency on dfe-engine

This is the point of the pull-based design. Once deployed, the running system needs
only ClickHouse:

- **Workers** read hunt defs from the gitops config (mounted / cloned), coordinate
  through the four CH tables, and execute queries against CH. No engine call on the
  hot path.
- **KEDA** reads `hunt_schedule` + the coordination tables from CH. No engine call.
- **The engine's only role is at CONFIG-DEPLOY time**, not runtime: materialise
  `hunt_schedule` when the hunt config changes. That runs as an Argo `PostSync` hook
  Job on the deploy (see below) - a one-shot, idempotent `publish_schedule`. It is
  invoked by the GitOps sync, not as a live service call.

So if the engine pod is down, hunts keep firing and KEDA keeps scaling. The only
thing you lose while the engine is down is the ability to CHANGE configs - which
needs the engine anyway. ClickHouse is DFE's single operational store; the runner
holds to that (no Postgres, no engine, no convenience store on the hot path), which
is also what lets the same runner work on a non-k8s single deploy (dfe-docker) where
only ClickHouse is guaranteed present.

### Coordination needs a SINGLE LOGICAL ClickHouse (scale-tier requirement)

The four coordination tables are the shared source of truth every worker and KEDA
read/write. That only works if all of them see the SAME tables. On a single-node CH
(docker / slim / single tiers) that is automatic. On the **scale tier's multi-node
cluster behind a round-robin load balancer, it is NOT**: a plain
`ReplacingMergeTree` (and a plain `CREATE DATABASE`) lands on one node, so workers
whose connections land on other nodes see an empty/absent table - coordination
silently breaks (surfaced as `UNKNOWN_DATABASE` / missing leases). So on a clustered
CH the coordination tables MUST be either:

- **Replicated** - `ReplicatedReplacingMergeTree` in a `Replicated`/`ON CLUSTER`
  database, so lease/watermark/schedule state is shared across nodes; or
- **pinned to a single endpoint** - point the runner + KEDA + the materialiser at one
  CH node (or a sticky service), so every connection hits the same tables.

`ensure_schema` currently creates plain engines (correct for the single-node tiers);
the scale-tier variant (Replicated engines, chosen by the data-substrate mode) is a
follow-up. The synthetic multi-pod integration test detects this and skips on a
multi-node endpoint rather than flaking (see tests/integration/test_hunt_synthetic).

### Materialisation trigger (config-deploy time)

`schedule.publish_schedule(ch, db, specs)` is the ONLY writer of `hunt_schedule`. It
is idempotent and diff-aware (tombstones hunts that vanished from config). Wire it
as an Argo `PostSync` hook Job in the hunt-runner app, so every deploy-repo sync
that changed the hunt config refreshes the schedule BEFORE (or as) the workers roll.
Because it runs at deploy time, a brand-new hunt added while scaled to zero still
gets a `hunt_schedule` row, so KEDA can wake a worker for it - no chicken-and-egg.

## KEDA ScaledObject (shape)

**KEDA has NO native ClickHouse scaler.** So we scale on the due backlog via KEDA's
**MySQL scaler pointed at ClickHouse's MySQL-compatible interface** (`mysql_port`,
default 9004). The query is still ClickHouse SQL, executed by CH - so KEDA still
reads CH DIRECTLY (no engine on the scaling path). Ships in the engine chart
(`chart/templates/hunt-runner-scaledobject.yaml`, gated by `huntRunner.keda.enabled`):

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: dfe-hunt-runner
spec:
  scaleTargetRef:
    name: dfe-hunt-runner
  minReplicaCount: 0            # scale to zero when due == 0
  maxReplicaCount: 8            # == the global CH cap the runner enforces
  pollingInterval: 30
  cooldownPeriod: 120
  triggers:
    - type: mysql              # NOT "clickhouse" - KEDA has no CH scaler
      metadata:
        host: <ch-host>
        port: "9004"           # ClickHouse mysql_port (must be enabled on CH)
        dbName: <data-database>
        username: <ch-user>
        queryValue: "1"        # ceil(due / 1) workers; tune hunts-per-worker here
        query: >-
          SELECT count() AS due FROM ( ... schedule.due_query(db) ... )
      authenticationRef:
        name: dfe-hunt-runner-auth   # CH password via TriggerAuthentication
```

Requirements + notes: ClickHouse must have `mysql_port` enabled. `due_query(db)` is
the source of truth for the `query` field - keep them identical (the unit test pins
the clauses so a drift fails CI). If enabling `mysql_port` is undesirable, the
fallback is a KEDA metrics-api scaler over a small always-on endpoint returning
`due_count` - but that reintroduces an HTTP dependency on the scaling path, so the
direct MySQL-scaler route is preferred.

## Disable = pod count 0

The hunt runner is deployed by dfe-infra and enabled by default (except the `slim`
tier - see docs deployment-tiers), but it is disableable like any DFE app:

- **Idle (automatic):** `minReplicaCount: 0` means the runner sits at zero replicas
  whenever `due_count == 0`, and KEDA brings it back on the next due fire. This is
  the normal resting state, not a disable.
- **Disabled (operator choice):** set the app's replica dial to disabled in the
  deploy overlay. With no ScaledObject (or `maxReplicaCount: 0`) the runner stays at
  zero and hunts do not fire. Re-enable by restoring the dial - the schedule +
  watermarks are still in CH, so it resumes cleanly from the last committed window.

Either way the mechanism is one dial, turned through the gitops overlay, never a
live cluster mutation.
