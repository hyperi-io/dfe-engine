# Data plane

The close-coupled source, ingest, schema and transform path. One source
definition holds the routing, the schema binding and the field-map bindings
together, and the engine compiles every stage's config from it, so there is no
separate ETL config to drift. The system map is
[../architecture.md](../architecture.md).

The engine compiles one source definition into receiver rules, loader table routing and the table with its views.

```mermaid
flowchart LR
    accTitle: L4 Detail -- What a source definition compiles into
    accDescr: The engine compiles one source definition into the receiver's routing rules, the loader's table routing and the table with its standard views. The hunt runner and the query API read the table and its views.

    source["Source<br/>definition"]

    engine["dfe-engine<br/>compilers"]

    receiver["Receiver<br/>rules"]
    loader["Loader<br/>table routing"]
    views[("Table and<br/>standard views")]

    hunts["Hunt runner"]
    query["Query API"]

    source --> engine
    engine --> receiver
    engine --> loader
    engine --> views
    views --> hunts
    views --> query

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef core fill:#DCEBF7,stroke:#0072B2,stroke-width:2px,color:#0B2A3F
    classDef store fill:#DDF3EC,stroke:#00785A,stroke-width:1px,color:#0B3327
    class source core
    class views store
```

*Figure 1. L4 Detail -- What a source definition compiles into.* Up: [L3 Components -- dfe-engine](../architecture.md).

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
| [expressions-cel.md](expressions-cel.md) | CEL profile, classifier tiers, SQL transpile |
| [query-api.md](query-api.md) | query API surface, and the [in-image Python client](query-api-python.md) |
| [hunt-runner-scaling.md](hunt-runner-scaling.md) | pull-based runner, deterministic-due scaling, keda-shim fail-safe |
| [hunt-match-delivery.md](hunt-match-delivery.md) | where a hunt match is written, who sees it, alert destinations and what the runner sends |
| [hunt-schedule-smoothing.md](hunt-schedule-smoothing.md) | phase-drift load smoothing (incl. the reference derivation) |
