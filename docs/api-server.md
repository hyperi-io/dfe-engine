# API server

The API server is the part of [dfe-engine](architecture.md) that every operator change goes through: it keeps the deployment's config, compiles what the data apps read, and applies it.

| | |
|---|---|
| Repo | [hyperi-io/dfe-engine](https://github.com/hyperi-io/dfe-engine) |
| Image and chart | `ghcr.io/hyperi-io/dfe-engine`, entry point `dfe-engine`. Chart `oci://ghcr.io/hyperi-io/charts/dfe-engine` |
| Config reference | `DFE_ENGINE_` environment variables, `src/dfe_engine/settings.py` |
| Endpoints | `:8000` the API under `/api/v1` ([route map](reference/api-route-map.md)). `:9090` `/livez`, `/readyz` and `/metrics` |
| Metrics | Prometheus `/metrics` and OTLP, names prefixed `dfe_` ([observability standard](deployment/observability-standard.md)) |

Every change enters through the routes and leaves as a commit or as DDL.

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
  accTitle: L3 Components -- API server
  accDescr: dfe-ui and the dfe CLI call the routes. The routes drive the config registries and the compilers. The GitOps writer commits registry changes and compiled overlays to the deploy repo, and the provisioners apply DDL to ClickHouse and create Kafka topics.
  subgraph CALLERS["Callers"]
    direction LR
    ui("<b>dfe-ui</b><br/><span>the DFE console</span>")
    cli("<b>dfe CLI</b><br/><span>every operation</span>")
  end
  subgraph API["API server"]
    direction TB
    routes("<b>Routes and auth</b><br/><span>/api/v1, RBAC</span>")
    registries("<b>Config registries</b><br/><span>sources, hunts, rules</span>")
    compilers("<b>Compilers</b><br/><span>routing, DDL, Sigma</span>")
    writer("<b>GitOps writer</b><br/><span>one commit a change</span>")
    provisioners("<b>Provisioners</b><br/><span>tables, topics, roles</span>")
    routes --> registries
    routes --> compilers
    registries --> writer
    compilers --> writer
    compilers --> provisioners
  end
  subgraph TARGETS["Deploy and store"]
    direction LR
    repo[("<b>Deploy repo</b><br/><span>git</span>")]
    ch[("<b>ClickHouse</b>")]
    kafka[("<b>Kafka</b><br/><span>optional</span>")]
  end
  CALLERS -->|calls| routes
  writer -->|commits| repo
  provisioners -->|DDL| ch
  provisioners -.->|topics| kafka
  class ui ui
  class cli,repo control
  class routes,registries,compilers,writer,provisioners part
  class ch store
  class kafka bus
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
  style CALLERS fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style API fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style TARGETS fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L3 Components -- API server. The dotted arrow runs only where the deployment has Kafka.* Up: [L2 Services -- dfe-engine](architecture.md). Down: none.

## How it works

1. Routes and auth authenticate the request by trusted OIDC headers, an API key or a bearer token, and check its RBAC scope.
2. The config registries read and write sources, hunts, rules, field maps, governance and app overlays. With GitOps on they live in the deploy repo, otherwise in directories under `DFE_CONFIG_DIR`.
3. The compilers turn a source into the receiver's routing rules, the loader's table map, the table's DDL and its Sigma view. Behind the `sigma.enabled` beta, off by default, they turn Sigma rules into SQL ([#887](https://github.com/hyperi-io/dfe-engine/issues/887)).
4. The GitOps writer makes one attributed commit per change.
5. The provisioners apply the dfe-schemas manifest and each source's table, create Kafka topics, and keep the dfe-hyperdx sources and the ClickHouse roles and row policies in step.

A source definition carries its routing rule, schema binding and field maps together, and every stage reads what is compiled from it.

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
flowchart LR
  accTitle: L4 Detail -- One source definition, compiled
  accDescr: A source definition goes to the compilers, which produce the receiver's rules, the loader's table map, the source's table and its Sigma view.
  src("<b>Source definition</b>")
  compilers("<b>Compilers</b><br/><span>in the API server</span>")
  subgraph OUT["Compiled from it"]
    direction TB
    rules("<b>Receiver rules</b>")
    tablemap("<b>Loader table map</b>")
    table[("<b>Source table</b>")]
    view[("<b>Sigma view</b>")]
    rules ~~~ tablemap ~~~ table ~~~ view
  end
  src -->|definition| compilers
  compilers --> OUT
  class table,view store
  class src,compilers,rules,tablemap part
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
  style OUT fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 2. L4 Detail -- One source definition, compiled.* Up: [L3 Components -- API server](api-server.md).

The `<table>_sigma` view projects the source's columns under Sigma field names for detection. Routing that disagrees with the sources is reported as drift and re-synced.

## Interfaces

- Inbound: HTTP on port 8000. The dfe CLI and dfe-ui's types are both generated from the OpenAPI specs in `openapi/`.
- Deploy repo: app overlays in `values/`, backing-service overlays in `infra/`, governance in `governance/`, and with GitOps on the sources in `config/sources/` and the hunts and rules in `config/hunts/` and `config/rules/`. GitHub, GitLab or the in-cluster Forgejo.
- ClickHouse: DDL, roles and row policies. The engine is the only thing that creates ClickHouse objects.
- Kafka: topic creation, on tiers that run Kafka.
- dfe-hyperdx: the sources the Observe view queries.

Adding a service instance is a commit of one values file. dfe-infra's ApplicationSet turns each file into one Argo CD Application.

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
flowchart LR
  accTitle: L4 Detail -- Instance deploy path
  accDescr: The GitOps writer commits an instance values file to the deploy repo. dfe-infra's ApplicationSet makes one Argo CD Application per values file, which renders the app's chart into service pods.
  subgraph REPO["Commit"]
    direction TB
    writer("<b>GitOps writer</b><br/><span>in the API server</span>")
    values[("<b>Instance values file</b>")]
    writer -->|commit| values
  end
  subgraph ARGO["Argo CD sync"]
    direction TB
    appset("<b>ApplicationSet</b>")
    app("<b>Argo Application</b>")
    pods("<b>Service pods</b>")
    appset --> app -->|helm| pods
  end
  REPO -->|matched| ARGO
  class values store
  class writer part
  class appset,app control
  class pods part
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
  style REPO fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style ARGO fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 3. L4 Detail -- Instance deploy path.* Up: [L3 Components -- API server](api-server.md).

The file is `values/<service>-<instance>-values.yaml`, and dfe-infra's git-files ApplicationSet (`argocd/appsets/layer2-apps.yaml`) matches it. Removing the file removes the app. An instance removed while its source still exists keeps its files in `values/<service>-<instance>-parked.yaml`, which the glob does not match, and its next deploy restores them.

## Scaling and failure

- Nothing on the data path or the hunt path calls it, so an API server that is down delays config changes and nothing else.
- A write that names the revision it read in `If-Match` gets `409 Conflict` with the current document when that revision is stale. A write with no `If-Match` is not checked.
- With no ClickHouse at start it waits up to 180 seconds for the schema bootstrap (`DFE_ENGINE_CLICKHOUSE_BOOTSTRAP_WAIT_SECONDS`), then serves with `/readyz` reporting `clickhouse: false`.
- The engine reports what it committed. Argo CD, and any approval gate, decides when it is live.

## Key metrics

`/readyz` reports `clickhouse: false` while ClickHouse is unreachable. `api_bearer_tokens_refused_total{reason}` counts refused bearer tokens. The [observability standard](deployment/observability-standard.md) lists the rest.

## Related

- [Control plane](control-plane/README.md): governed GitOps, RBAC and OIDC in depth.
- [Source definition](data-plane/source-definition.md) and [schema](data-plane/schema.md).
- [Deployment](deployment/README.md): the tiers and the deploy-repo providers.
