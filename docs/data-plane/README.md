# Data plane

The close-coupled source, ingest, schema and transform path. One source
definition holds the routing, the schema binding and the field-map bindings
together, and the engine compiles every stage's config from it, so there is no
separate ETL config to drift. The system map is
[../architecture.md](../architecture.md).

The engine compiles one source definition into receiver rules, loader table routing and the table with its views.

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
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart TB
  accTitle: L4 Detail -- What a source definition compiles into
  accDescr: The engine compiles one source definition into the receiver's routing rules, the loader's table routing and the table with its standard views. The hunt runner and the query API read the table and its views.

  source["<b>Source definition</b>"]

  engine["<b>dfe-engine</b><br/><span>compilers</span>"]

  subgraph compiled["Compiled"]
    direction LR
    receiver["<b>Receiver rules</b>"]
    loader["<b>Loader table routing</b>"]
    views[("<b>Table</b><br/><span>and standard views</span>")]
  end

  subgraph readers["Readers"]
    direction LR
    hunts["<b>Hunt runner</b>"]
    query["<b>Query API</b>"]
  end

  source --> engine
  engine --> receiver
  engine --> loader
  engine --> views
  views --> readers
  class views store
  class engine control
  class source,receiver,loader,hunts,query part
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
  style compiled fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style readers fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- What a source definition compiles into.* Up: [L3 Components -- API server](../api-server.md).

## Documents in this area

| Doc | Covers |
|---|---|
| [source.md](source.md) | Source as the top-level data abstraction |
| [source-definition.md](source-definition.md) | the source YAML field by field, and what a write is validated against |
| [source-registry.md](source-registry.md) | states, storage backends, CRUD, topics on deploy, the API surface |
| [source-flow.md](source-flow.md) | INPUT, optional TRANSFORM, OUTPUT on the Kafka or gRPC transport. What compiles from a source. How a change reaches a pod |
| [source-routing.md](source-routing.md) | the exact receiver and loader config a source compiles into |
| [topic-contract.md](topic-contract.md) | the Kafka topics a source implies: ensure, converge, status, remove, and the dials |
| [external-pipelines.md](external-pipelines.md) | the customer's own Vector, Fluentd or Logstash as a source's origin or transform on Kafka |
| [source-detections.md](source-detections.md) | rules, hunts and Sigma views on a source |
| [schema.md](schema.md) | schema type system: primitives, attributes, use cases, DDL |
| [schema-classes.md](schema-classes.md) | engine class reference for the schema system (SchemaLoader, SchemaBuilderV2, DDL generation) |
| [schema-sync.md](schema-sync.md) | engine<->loader runtime schema contract |
| [field-mapping.md](field-mapping.md) | standards translation layer (Sigma / ECS / CIM views) |
| [sigma.md](sigma.md) | Sigma rule-provider pipeline (feeds -> catalogue -> propagation -> source-views) + query cost leaderboard |
| [expressions-cel.md](expressions-cel.md) | [CEL](https://github.com/cel-expr/cel-spec/blob/master/doc/langdef.md) profile, classifier tiers, SQL transpile |
| [query-api.md](query-api.md) | query API surface, and the [in-image Python client](query-api-python.md) |
| [hunt-runner-scaling.md](hunt-runner-scaling.md) | pull-based runner, deterministic-due scaling, keda-shim fail-safe |
| [hunt-match-delivery.md](hunt-match-delivery.md) | where a hunt match is written, who sees it, alert destinations and what the runner sends |
| [hunt-schedule-smoothing.md](hunt-schedule-smoothing.md) | phase-drift load smoothing (incl. the reference derivation) |
