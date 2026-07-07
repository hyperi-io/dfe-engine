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
with no worker running - i.e. from durable state, not a worker's memory. But
the per-hunt schedule (interval + stable phase offset) otherwise lives only in a
running worker's memory (loaded from the gitops hunt configs). At zero workers
there is nobody to ask.

So the schedule is MATERIALISED into a small ClickHouse table, `hunt_schedule`, at
config-deploy time. The due-query then reads it directly (the engine runs that query
on KEDA's behalf - see "KEDA ScaledObject" below). Three tables already exist
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

The engine runs `schedule.due_query(db)` against ClickHouse and returns the count of
hunts that are due-and-unclaimed; KEDA reads that count from the engine's
`/api/v1/system/hunts-due` endpoint (see "KEDA ScaledObject" below). The query uses
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
3. **Gate on the signal, not a timer.** KEDA polls the engine's backlog count (a
   real readiness signal), it does not race a clock. Worst-case wake latency is
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

## Worker hot-path independence from dfe-engine

The pull-based design keeps the WORKERS free of any engine dependency at runtime -
once deployed they need only ClickHouse:

- **Workers** read hunt defs from the gitops config (mounted / cloned), coordinate
  through the four CH tables, and execute queries against CH. No engine call on the
  hot path.
- **The engine's role at CONFIG-DEPLOY time**, not on the worker hot path:
  materialise `hunt_schedule` when the hunt config changes. That runs as an Argo
  `PostSync` hook Job on the deploy (see below) - a one-shot, idempotent
  `publish_schedule`. It is invoked by the GitOps sync, not as a live service call.
- **KEDA reads the due count from the engine** at scaling time: its `metrics-api`
  scaler polls `GET /api/v1/system/hunts-due` (below). This is the one place the
  scaling path touches the engine - a deliberate trade. Rather than open a ClickHouse
  wire port (`mysql_port` / `postgresql_port`) purely so KEDA can count rows, we let
  the engine (which already holds the CH connection and is the authority on what is
  due) run the deterministic due-query and hand back `{"due": N}`. It is a cheap
  read-only poll every `pollingInterval`.

So if the engine pod is down, **in-flight and already-scheduled workers keep firing
hunts** (they never call the engine), but KEDA cannot change the replica count -
scale-out and scale-from-zero pause until the engine answers again, and any resident
worker at >=1 replica keeps the backlog draining meanwhile. The count call itself
fails SAFE to `due=0` on a transient CH error, so a CH blip never spuriously scales
up or blocks scale-to-zero. ClickHouse remains DFE's single operational STORE (no
Postgres, no convenience store on the hot path), which is also what lets the same
runner work on a non-k8s single deploy (dfe-docker) where only ClickHouse is
guaranteed present.

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

**KEDA has NO native ClickHouse scaler.** Rather than open a CH wire port
(`mysql_port` / `postgresql_port`) purely so KEDA can count rows - a shared-infra +
security-surface change for one workload - we scale on the due backlog via KEDA's
stock **`metrics-api` scaler** pointed at the engine's own
`GET /api/v1/system/hunts-due` endpoint. The engine already holds the CH connection
and is the authority on what is due, so it runs `schedule.due_query(db)` and returns
`{"due": N}`; KEDA reads the count from the `due` field. No CH port, no Prometheus,
no custom scaler component. Ships in the engine chart
(`chart/templates/hunt-runner-scaledobject.yaml`, gated by `huntRunner.keda.enabled`),
which also emits the `TriggerAuthentication` carrying the engine API key:

```yaml
apiVersion: keda.sh/v1alpha1
kind: TriggerAuthentication
metadata:
  name: dfe-engine-hunt-runner-auth
spec:
  secretTargetRef:
    - parameter: apiKey            # the engine API key (scope hunt:read)
      name: <apiKeySecretName>     # default: the engine managed secret
      key: keda-hunt-scaler-api-key
---
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: dfe-engine-hunt-runner
spec:
  scaleTargetRef:
    name: dfe-engine-hunt-runner
  minReplicaCount: 0            # scale to zero when due == 0
  maxReplicaCount: 8            # == the global CH cap the runner enforces
  pollingInterval: 30
  cooldownPeriod: 120
  triggers:
    - type: metrics-api        # NOT "clickhouse" - KEDA has no CH scaler
      metadata:
        url: "http://dfe-engine:8000/api/v1/system/hunts-due"
        valueLocation: "due"           # GJSON path into {"due": N}
        targetValue: "1"               # hunts-per-worker; desiredReplicas = ceil(due / targetValue)
        activationTargetValue: "0"     # activate from zero whenever due > 0 (strict >)
        authMode: "apiKey"
        method: "header"
        keyParamName: "X-API-Key"      # KEDA sends the engine API key in this header
      authenticationRef:
        name: dfe-engine-hunt-runner-auth
```

Notes:
- **`activationTargetValue: "0"` is load-bearing.** KEDA activates from zero when the
  metric is STRICTLY greater than `activationTargetValue`, so `0` means "wake on any
  due hunt" (`due >= 1`). `"1"` would need `due >= 2`, and a single due hunt would
  never wake a worker. Proven in the scratch-cluster KEDA scale test (0 -> 1 -> 0).
- **Auth.** KEDA sends an engine API key (scope `hunt:read`) as the `X-API-Key`
  header. Provision the key, store it in the secret the `TriggerAuthentication`
  references (`huntRunner.keda.apiKeySecretName` / `apiKeySecretKey`), and confirm the
  `metrics-api` `method` / `keyParamName` field names against the deployed KEDA version.
- **`due_query(db)` is the source of truth** for the count the endpoint returns - the
  unit test pins its clauses so a drift fails CI. The endpoint fails SAFE to `due=0`
  on a transient CH error, so a blip never spuriously scales up or blocks scale-to-zero.

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
