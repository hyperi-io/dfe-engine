# Hunt runner

The hunt runner is the part of [dfe-engine](architecture.md) that runs each scheduled hunt inside ClickHouse and tells someone when it matched.

| | |
|---|---|
| Repo | [hyperi-io/dfe-engine](https://github.com/hyperi-io/dfe-engine) |
| Image and chart | `ghcr.io/hyperi-io/dfe-engine`, command `dfe-hunt-runner run`. dfe-infra chart `helm/charts/dfe-extras`, `templates/engine-hunt-runner.yaml` |
| Config reference | `DFE_ENGINE_HUNTS_` environment variables (`src/dfe_engine/settings.py`), and the chart's `huntRunner` values |
| Endpoints | None. The runner has no HTTP listener |
| Metrics | `dfe_hunt_*`, pushed over OTLP ([observability standard](deployment/observability-standard.md#the-hunt-runner-pushes-its-metrics)) |

A hunt needs only the deploy repo and ClickHouse to fire.

```mermaid
---
config:
  theme: base
  look: neo
  fontFamily: Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 64
    padding: 18
    wrappingWidth: 280
    htmlLabels: true
  elk:
    preset: modelOrder
  themeVariables:
    fontSize: 15px
    lineColor: "#64748B"
    primaryTextColor: "#0F172A"
    nodeBorder: "#334155"
    clusterBorder: "#0072B2"
    edgeLabelBackground: "#FFFFFF"
  themeCSS: >-
    p{margin:0}
    .node[data-look] rect,.node[data-look] path,.node[data-look] polygon,.node[data-look] .outer-path{filter:drop-shadow(0 1px 2px rgba(15,23,42,.10)) drop-shadow(0 6px 14px rgba(15,23,42,.10))}
    .node rect{rx:10px;ry:10px}
    .cluster rect{rx:16px;ry:16px}
    .node .label div{min-width:150px}
    .node.decision .label div{min-width:0}
    .cluster[data-look] rect{filter:none}
    .nodeLabel{line-height:1.35}
    .nodeLabel b{font-weight:600;font-size:15px;letter-spacing:-0.01em}
    .nodeLabel span{font-size:12.5px;font-weight:400;color:#334155}
    .cluster-label span.nodeLabel{display:inline-block;background:#FFFFFF;color:#0B2A3F;border:1.5px solid #0072B2;border-radius:999px;padding:2px 12px;font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
    .flowchart-link{stroke-width:1.75px}
    .labelBkg{background:transparent!important}
    span.edgeLabel{display:inline-block;background:#FFFFFF!important;color:#1E293B;border:1px solid #94A3B8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500}
    span.edgeLabel p{background:transparent!important}
    .label-icon{width:20px;height:20px;vertical-align:-4px;margin-right:6px}
    .icon-shape path[stroke-width="0"][fill="none"]{fill:#FFFFFF;stroke:#FFFFFF;stroke-width:12px;stroke-linejoin:round}
    .icon-shape p{background:transparent!important;color:#0F172A}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart LR
  accTitle: L3 Components -- Hunt runner
  accDescr: A git-sync sidecar brings the hunts and rules from the deploy repo to the hunt runner. Its scheduler claims due fires, the worker runs them, and the notifier sends alerts. The runner writes to ClickHouse and the alert destinations.
  repo[("<b>Deploy repo</b><br/><span>hunts and rules</span>")]
  subgraph RUNNER["Hunt runner"]
    direction TB
    sched("<b>Scheduler</b><br/><span>due fires, leases</span>")
    worker("<b>Worker</b><br/><span>windowed INSERT</span>")
    notifier("<b>Notifier</b><br/><span>one message a fire</span>")
    sched --> worker --> notifier
  end
  subgraph OUT["Writes to"]
    direction TB
    ch[("<b>ClickHouse</b><br/><span>detections, state</span>")]
    dest("<b>Alert destinations</b><br/><span>Apprise URLs</span>")
    ch ~~~ dest
  end
  repo -->|git-sync| RUNNER
  RUNNER --> OUT
  class repo control
  class sched,worker,notifier part
  class ch store
  class dest ext
  classDef ext fill:#F4F6F8,stroke:#94A3B8,stroke-width:1.5px,stroke-dasharray:5 4,color:#0F172A
  classDef ingest fill:#FFFFFF,stroke:#0E8A7D,stroke-width:3px,color:#0F172A
  classDef xform fill:#FFFFFF,stroke:#7B4FB0,stroke-width:3px,color:#0F172A
  classDef load fill:#FFFFFF,stroke:#0072B2,stroke-width:3px,color:#0F172A
  classDef bus fill:#FFFFFF,stroke:#A8517F,stroke-width:3px,color:#0F172A
  classDef store fill:#FFFFFF,stroke:#00785A,stroke-width:3px,color:#0F172A
  classDef ui fill:#FFFFFF,stroke:#D9480F,stroke-width:3px,color:#0F172A
  classDef decision fill:#FFFFFF,stroke:#9A6700,stroke-width:3px,color:#0F172A
  classDef error fill:#FFFFFF,stroke:#C2410C,stroke-width:3px,color:#0F172A
  classDef control fill:#FFFFFF,stroke:#334155,stroke-width:3px,color:#0F172A
  classDef part fill:#FFFFFF,stroke:#94A3B8,stroke-width:1.5px,color:#0F172A
  style RUNNER fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style OUT fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L3 Components -- Hunt runner. The dashed grey box is outside DFE.* Up: [L2 Services -- dfe-engine](architecture.md). Down: none.

## How it works

1. The runner loads each hunt file from the synced `config/hunts/` directory, and the file's name is the hunt's id. A hunt that names rules gets one `INSERT ... SELECT` per rule, compiled at load, so a hunt made in dfe-ui runs with no hand-written SQL.
2. Every poll (15 seconds, `huntRunner.poll`) the scheduler finds the fires that are due. A hunt fires once per interval at a stable offset inside the first 80% of it (`hunts.phase_spread_fraction`), so hunts do not all start at once.
3. A due fire waits until every ClickHouse replica holds its window, then the scheduler claims a lease on it in ClickHouse. At most 8 fires run at once across all runner pods (`huntRunner.cap`), so ClickHouse is never flooded.
4. The worker runs the fire's statements over its window, writing matches to the hunt's target table, `detection` by default. It advances the watermark only after they commit.
5. The notifier sends one message per fire whose `alerts` trigger holds. A failed send is logged and the fire stands, because its rows are already written.

## Interfaces

- In: hunt and rule YAML from the deploy repo's `config/hunts/` and `config/rules/`, through a git-sync sidecar.
- In: alert destination URLs, read from the engine's secrets backend on every fire.
- ClickHouse: the source tables it reads, the detection table it writes, and the coordination tables `hunt_lease`, `hunt_watermark`, `hunt_state`, `hunt_run`, `hunt_runner_heartbeat` and `hunt_schedule`.
- Out: Apprise messages to Slack, email, PagerDuty, Teams and the other services Apprise covers.

## Scaling and failure

- One steady pod covers most deployments, since the query load runs in ClickHouse. A second pod competes for the same leases and nothing runs twice.
- A pod that dies mid-fire loses its lease after 300 seconds, and another pod re-runs the window from the last committed watermark. Nothing is lost and nothing is skipped.
- A ClickHouse that is down or full never ends the process. Each tick that hits it is skipped until the next poll.
- A failed fire keeps its watermark, so it stays due. It is retried after 30 seconds, doubling to 600.
- A rule writes at most 1,000 matches a fire (`hunts.max_detections_per_run`), then one summary row with the true count.
- With the API server down, a running pod keeps firing hunts. A pod that is starting waits for it.

## Key metrics

| Metric | Healthy |
|---|---|
| `dfe_hunt_runs_total{outcome}` | Rising, mostly `success` |
| `dfe_hunt_backlog` | Near 0 between fires |
| `dfe_hunt_fire_failures_total{reason}` | Flat |
| `dfe_hunt_clickhouse_unavailable_total` | Flat |
| `dfe_hunt_detections_capped_total` | Flat. A rise means a rule matches more than its cap |

## Related

- [Hunt-runner scaling](data-plane/hunt-runner-scaling.md): the leases, the schedule table and KEDA.
- [Hunt schedule smoothing](data-plane/hunt-schedule-smoothing.md).
- [How a hunt match reaches a person](data-plane/hunt-match-delivery.md).
- [Source detections](data-plane/source-detections.md) and [Sigma](data-plane/sigma.md).
