<!--
  Project:   dfe-engine
  File:      docs/data-plane/hunt-schedule-smoothing.md
  Purpose:   Adaptive load-smoothing for rate (loop-query) hunts on ClickHouse
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Hunt schedule smoothing - flattening CH load by phase drift

How DFE keeps a fleet of recurring detection queries from stacking up into CPU and
memory peaks on a shared ClickHouse cluster, by learning the load each one imposes
and drifting *when* (not how often) it fires so the aggregate curves flatten over
time. This is a closed-loop **remediation** approach, not admission control: we do
not gate or defer firing - we nudge phases and let the fleet settle.

> **Status: BETA (opt-in, experimental).** Like some of the beta dfe-receiver transports,
> the adaptive smoothing loop is AVAILABLE but opt-in and still settling: `smoothing.enabled`
> defaults OFF, the actuator (the phase offset materialised into `hunt_schedule`) and the
> cost tagging ship today, and the closed loop is turned on deliberately per deployment.
> Treat its knobs and behaviour as subject to change until it graduates out of beta.

## The picture (intuition first)

The whole job is one shape change. On the left, a fleet of recurring hunts that all fire
at the top of their period, so their cost pulses STACK into a few tall peaks on the shared
ClickHouse, with idle troughs between. On the right, after the loop drifts each hunt's
phase (WHEN it fires, never how often), the same total work SPREADS into the troughs and
the aggregate curve FLATTENS. Same coverage, same freshness - just no synchronised pile-ups.

```
Aggregate ClickHouse pressure P(b) across one hyperperiod H   (b = one Delta-second bucket)

  BEFORE - all fire on the period boundary          AFTER - phases drifted apart

  P |   #                                           P |
    |   #                                             |    x   x   x   x   x
    |   #          #                                  |  x # x # x # x # x # x
    |   #          #          #                       |  # # # # # # # # # # #
    | x # x      x # x      x # x                     |  # # # # # # # # # # #
    +------------------------------> b                +------------------------------> b
      peak/mean ~ 4  (spiky: risks the                  peak/mean ~ 1.2  (flat: headroom
      memory cliff)                                     under the memory cap)
```

The one number that tracks this is `dfe_hunt_smoothing_flatness` = peak/mean, falling
toward 1.0 as the curve flattens; the objective J (in the maths section) is just the
formal handle on "make this flat". If you read one section, read this one - the maths
only proves the drift provably lowers those peaks and settles.

## Scope

- **In scope: rate hunts (loop-queries)** - the "run every N minutes/hours"
  detections (see [HUNT-RUNNER-SCALING.md](hunt-runner-scaling.md), `mode: rate`).
  These get phase-offset spreading and the adaptive smoothing below.
- **Out of scope: anchored hunts and monster one-offs.** The deliberately-spiky
  `mode: anchored` runs (e.g. a new TI checked against 270 days of history at a
  weekend lull) are *meant* to spike, and sizing the cluster for them is the infra
  team's problem, not the scheduler's. The smoother ignores them (beyond keeping
  their memory off the cliff via the guard below).

## The two drivers (this is huge OLAP, so both matter, differently)

A recurring query stresses two resources with two very different failure modes:

- **CPU** - heavy calculation / transform / aggregation. A CPU peak makes queries
  *slow*. Soft and recoverable.
- **Memory** - big scans, joins, large `GROUP BY`. When several land together and
  cross the server memory limit, ClickHouse throws `MEMORY_LIMIT_EXCEEDED` (code 241)
  at whatever is allocating right then - so a memory spike **fails in-flight queries,
  innocent bystanders included**, not just the offenders. That is an availability
  cliff, not a node death: with sane limits (`max_server_memory_usage` leaving OS
  headroom + per-query `max_memory_usage`) ClickHouse sheds queries and STAYS UP -
  shedding queries is exactly what the server limit is for. A true OS/cgroup OOM-KILL
  of the CH process (a crash/restart) is a separate, catastrophic mode, but it takes
  a misconfiguration (no OS headroom, wrong cgroup detection, limits above real RAM),
  not merely concurrent heavy queries.

So the smoother optimises a weighted composite of both, but memory still gets extra
protection: its failure is a cliff (a spike fails *many* co-running queries at once),
not a slope (CPU just slows), so memory is a hard constraint backed by CH's per-query
`max_memory_usage` guard (below).

Visually the two failure modes could not be more different - which is why memory is the
hard constraint and CPU is only weighted:

```
  CPU  (soft slope: it just slows)          MEMORY  (hard cliff: sheds many at once)

  p95 latency                               query success
    |             _.-'                          |--------------.
    |         _.-'                              |              |
    |     _.-'                                  |              |
    |_.-'                                       |              '--------  code 241
    +------------------> load                   +------------------> load
    degrades gently, recoverable               fine ... fine ... then a spike fails
                                               the co-runners allocating right then
```

## The mechanism (remediation loop, non-destructive)

1. Fire what the operator asked for (their cron/interval - untouched).
2. Tag and observe each query's real cost over time from ClickHouse.
3. Fix overlaps, then incrementally drift phases so the CPU and memory curves
   flatten.
4. Commit the adjusted phases to the gitops repo (auditable); the runners reconcile
   on their next config reload (no restart - it is pull-based, nothing is pinned to
   a pod).
5. **Settling:** once a hunt's schedule has stopped moving for a configured period,
   promote the settled values *over* the operator's original guess in git (default
   ON - see Config).

Phase drift is deliberately non-destructive: it changes only *when within its
period* a hunt fires, never how often, so coverage and freshness are unchanged. The
phase offset was a hash guess to begin with, never operator intent - so learning a
better one and eventually committing it loses nothing.

---

## Derek's maths geek section

> With apologies for Derek's maths-geek section here

### Setup

Index the rate hunts i = 1..N. Each has period T_i (seconds) and a phase offset
φ_i ∈ [0, T_i) - the free variable we control. The **hyperperiod** is
H = lcm(T_1, ..., T_N); for the usual harmonic periods (300, 900, 3600 s) that is
just 3600 s. Discretise H into B buckets of width Δ = H / B (e.g. Δ = 30 s). Within
H, hunt i fires H / T_i times, at bucket positions ⌊(φ_i + k·T_i) / Δ⌋ mod B for
k = 0, 1, ....

From `system.query_log` (per hunt, p95) we learn a **cost pulse**: a duration d_i
buckets wide carrying a CPU magnitude c_i and a peak-memory magnitude m_i. A fire at
offset φ deposits that pulse across the d_i buckets it covers (wrapping around H).

### Aggregate curves

For each bucket b = 0..B-1, summing every fire of every hunt that covers b:

    C(b) = Σ_i Σ_k c_i · 𝟙[fire (i,k) covers b]      (CPU)
    M(b) = Σ_i Σ_k m_i · 𝟙[fire (i,k) covers b]      (memory)

Normalise each by cluster capacity: c̃(b) = C(b) / C_cap, m̃(b) = M(b) / M_cap.
The **composite pressure** in bucket b, with weights w_c + w_m = 1:

    P(b) = w_c · c̃(b) + w_m · m̃(b)

### Objective

Minimise the sum of squares of the composite pressure over the hyperperiod:

    minimise   J(φ) = Σ_{b=0}^{B-1} P(b)²
    subject to φ_i ∈ [0, T_i)           for all i        (full-period slack)
               M(b) ≤ M_cap             for all b        (keep concurrent memory under the server limit)

**Why sum-of-squares is the flattening objective.** The total pressure
T = Σ_b P(b) is *invariant* under phase changes - moving φ_i only shifts where a
hunt's pulses land, not their total (H is periodic, so pulses wrap, nothing falls
off the edge). Since

    Σ_b P(b)² = T²/B + Σ_b (P(b) − P̄)²  = const + B · Var(P),

minimising J is *exactly* minimising the variance of the load curve - i.e.
flattening it. And by Cauchy-Schwarz, J ≥ T²/B with equality iff P(b) is constant
(perfectly flat). This is the same objective the two source fields use (resource
smoothing's Σ R_t²; valley-filling's ℓ2-norm minimisation).

### The optimiser - damped, one-at-a-time water-filling

We do NOT globally re-solve (that thrashes and risks the herding "avalanche" the
demand-response literature warns of, where every load jumps to the same valley and
makes a new peak). Instead, each control cycle:

    1. Observe C, M from CH; refresh (c_i, m_i, d_i) from query_log p95.
    2. Compute P(b) for the current φ.
    3. b*  ← argmax_b P(b)                              (the current peak)
    4. i*  ← the hunt firing into b* with the largest marginal term in J
    5. φ*  ← the offset for i* that most reduces J (toward the deepest feasible
             trough), CLAMPED to a bounded step |φ* − φ_{i*}| ≤ Δφ_max, and
             rejecting any move with M(b) > M_cap in any bucket
    6. If ΔJ < −θ (improves by more than the hysteresis margin): commit φ* for i*
    7. Wait one cycle (let observation catch up); repeat.

Moving one hunt at a time, by a bounded step, with a hysteresis margin θ and a
per-hunt cool-down, is what makes it the gentle "drift" - and what prevents
oscillation and avalanche.

**Convergence.** Every accepted move strictly decreases J, and J ≥ 0 is bounded
below, so J converges monotonically. This is coordinate descent on J; it reaches a
local minimum, which - as the resource-leveling heuristics find in practice - is
where nearly all the benefit lives (the first few moves kill the worst peaks).

### Settling and promotion

Let the effective (learned) schedule for hunt i live in a gitcrud overlay separate
from the operator's committed def. Hunt i is **settled** when no move has been
accepted for it in a window ≥ X (the settling period). On settling, if promotion is
enabled (default), commit the settled (φ_i, and any backed-off T_i from the
runs-into-itself loop) *over* the operator's original values in git - with an audit
message - and clear i's overlay entry. Future drift then starts from the settled
baseline. This keeps the committed config truthful (it reflects what actually runs
well, not the initial hash guess) and bounds overlay growth.

---

## Cost model

Attribution needs one cheap enabler: every hunt query carries
`SETTINGS log_comment = 'hunt:<id>', workload = 'hunts'`. Then per hunt, over a
trailing window:

    c_i ≈ p95( ProfileEvents CPU µs )      from system.query_log (type = QueryFinish)
    m_i ≈ p95( peak_memory_usage )         "
    d_i ≈ p95( query_duration_ms ) / Δ     "

Because it is a trailing p95 refreshed each cycle, the model tracks data-shape drift
automatically - a table that grows or a join that gets heavier simply raises c_i/m_i
and the smoother re-drifts. This same telemetry feeds the interval-backoff loop
(runs-into-itself) described in HUNT-RUNNER-SCALING.md.

## ClickHouse-native backstop (defence in depth)

The smoother *optimises* the curve proactively; it can still mis-predict when data
shape jumps. So hunt queries also run inside a ClickHouse `WORKLOAD 'hunts'` with a
weight (fair-share against ingestion / UI) and, per query, `max_memory_usage`. That
gives a hard ceiling and reactive fair-share independent of our loop: our maths
shapes the load, CH guarantees no single hunt query exceeds its memory cap - so a
bad one fails on its own rather than starving co-runners or feeding an OS OOM-kill.
The two compose - proactive shaping plus reactive guard - and the CH tag is the same
`workload='hunts'` we set for attribution anyway.

## Metrics (scalo) - the controller is fully observable

A control loop you cannot see is one you cannot trust. Every input the smoother
decides on, every change it makes, and everything it gives up on is emitted as a
scalo metric through the standard OTel seam (see
[OBSERVABILITY-STANDARD.md](../deployment/observability-standard.md); default sink HyperDX). Three
buckets:

**1. What it is running** (runner/daemon operational state):
- `dfe_hunt_runner_due` - the due-but-unclaimed backlog (also the KEDA signal),
  `dfe_hunt_runner_active_leases`, `..._claimed_total`, `..._executed_total`,
  `..._deferred_total` (overrun), `..._lease_reclaims_total`, `..._tick_seconds`.

**2. What it changes** (the actuations - so every adjustment is auditable in metrics
as well as in git):
- `dfe_hunt_phase_shift_total{hunt}` + a `dfe_hunt_phase_offset{hunt}` gauge,
- `dfe_hunt_interval_backoff_total{hunt}` + `dfe_hunt_interval_seconds{hunt}`,
- `dfe_hunt_schedule_promotions_total{hunt}` (settling promotions),
- `dfe_hunt_autodisabled{hunt,reason}` (see below).

**3. The combined/derived metrics it decides ON** - the novel bit: the controller's
own inputs and objective are first-class metrics, so you can watch it work and prove
it converges:
- per-hunt cost model (trailing p95): `dfe_hunt_cost_cpu_us{hunt}`,
  `dfe_hunt_cost_mem_bytes{hunt}`, `dfe_hunt_cost_duration_ms{hunt}`,
- the aggregate curves: `dfe_hunt_load_cpu`, `dfe_hunt_load_mem`,
  `dfe_hunt_load_pressure` (the composite P) - each as peak, mean, and peak/mean,
- the objective: `dfe_hunt_smoothing_objective` (J = Σ P²) and
  `dfe_hunt_smoothing_flatness` (peak/mean, -> 1.0 as it flattens). J trending down
  is the proof the loop is working; J low-and-flat is "settled".

Failure metrics (they gate auto-disable): `dfe_hunt_mem_limit_total{hunt}`,
`dfe_hunt_mem_limit_crashloop_total{hunt}`, `dfe_hunt_threshold_breach_total{hunt,resource}`.

## What cannot be fixed - escalation and auto-disable

Rescheduling only fixes *contention*. A query that busts the memory limit will bust
it at 3am as surely as at peak - moving it changes nothing. Such hunts must be flagged and
stopped, not smoothed forever. The escalation ladder:

1. **Smooth** (phase drift) - resolve co-scheduling peaks. Fixes contention.
2. **Back off** (interval) - give a slow hunt room when it runs into itself. Fixes a
   merely-slow hunt.
3. **Auto-disable** - if it still fails, it is unfixable by scheduling: stop it and
   flag a human. The terminal "cannot be fixed" state.

Auto-disable triggers:

- **Memory-limit crash-loop (default ON - a safety, not opt-in).** A hunt whose query
  trips `MEMORY_LIMIT_EXCEEDED` (CH code 241) K times within a window is auto-disabled.
  A query that keeps blowing the memory limit fails itself AND the co-running queries
  allocating at that moment, so *retrying* it just repeats the collateral damage - we
  stop it. This is the last line behind the per-query
  `max_memory_usage` guard: the guard kills the one query; if it keeps happening, we
  kill the schedule.
- **Consumption thresholds (opt-in, exposed by API).** An operator sets, per-hunt or
  globally, a max peak-memory / max CPU / max duration; a breach auto-disables (or
  warn-then-disable). Governed-ops config - set/get via the API, committed to git.

Mechanics (consistent with the rest of the architecture):
- Auto-disable is a **governed-ops action**: write `enabled: false` + reason +
  timestamp to the hunt's state overlay in gitcrud (an auditable "why"), which the
  runners reconcile and KEDA stops counting. It is a **circuit-breaker**, distinct
  from the transient defer/overrun signal - defer self-heals; auto-disable stays open
  until a **human** fixes the query (or raises the threshold) and re-enables via the
  API. It never silently re-enables; the fault is not self-healing.
- Every auto-disable emits `dfe_hunt_autodisabled{hunt,reason}` AND raises an alert -
  exactly what alerting should surface: a detection was killed and is no longer
  running.

## Config knobs

| Knob | Default | Meaning |
|---|---|---|
| `smoothing.enabled` | off (v1) | master switch for the adaptive loop |
| `smoothing.weights` | `{cpu: 0.5, mem: 0.5}` | w_c / w_m in the composite P |
| `smoothing.bucket_seconds` | 30 | Δ, the curve resolution |
| `smoothing.max_step` | (period-relative) | Δφ_max, the per-cycle drift clamp |
| `smoothing.hysteresis` | small | θ, the min ΔJ to accept a move |
| `smoothing.settling_period` | on, X = 7 d | after X settled, promote learned schedule over the original in git |
| `smoothing.memory_cap` | from CH | M_cap for the hard constraint; also set CH `max_memory_usage` |
| `autodisable.mem_crashloop` | on, K=3 / 1 h | auto-disable a hunt that trips the memory limit K times in the window (safety, not opt-in) |
| `autodisable.max_memory` | off (opt-in) | per-hunt/global peak-memory ceiling -> disable on breach (API-set) |
| `autodisable.max_cpu` | off (opt-in) | CPU ceiling -> disable on breach (API-set) |
| `autodisable.max_duration` | off (opt-in) | duration ceiling -> disable on breach (API-set) |

## Where it sits

This is the v3 capability. v1 ("make the runner runnable") ships the *actuator* (the
phase offset is already materialised into `hunt_schedule`) and the *tagging*
(`log_comment` + `workload`), so the smoother drops in later once the cost model (v2)
exists - no rework of the runner or the KEDA path.

## Useful Links and References 

- **Resource smoothing** (operations research / project scheduling): shift tasks
  within slack to flatten a resource histogram; classic objective Σ_t R_t², via
  right/left-shift heuristics and MINSLK -
  [PMI](https://www.pmi.org/learning/library/time-constrained-approach-resource-leveling-1737).
- **Valley-filling / demand response** (smart grid): flatten aggregate demand by
  minimising the ℓ2-norm; decentralised water-filling one load at a time against a
  shared signal, with the avalanche caveat - this one is pretty cool, sometimes
  a side study pays off in places you least expect -
  [decentralised EV charging](https://arxiv.org/pdf/1710.05533),
  [survey](https://arxiv.org/pdf/1911.06500).
- **Adaptive periodic spreading** (systems): APIO spreads periodic I/O and extends
  the period under contention (our interval-backoff) -
  [APIO](https://www.mdpi.com/2079-9292/11/9/1318).
- **ClickHouse-native** workload / CPU-slot scheduling + concurrency/memory caps -
  the reactive backstop, not the arrival-time shaper -
  [CH docs](https://clickhouse.com/docs/operations/workload-scheduling).
