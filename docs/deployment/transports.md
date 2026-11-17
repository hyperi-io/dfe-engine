<!--
  Project:   dfe-engine
  File:      docs/deployment/transports.md
  Purpose:   Choosing a profile and a transport. The bus providers
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Transports and profiles

DFE moves records between stages on one of two transports: a data bus, or direct gRPC. The profile picks which one, and every stage is bound to it at deploy - on the bus the loader consumes the topic, on direct it listens for a push, and no stage does both. So a deployment runs one transport, `GET /api/v1/system/deployment` reports that one, and a source naming the other is refused at save rather than accepted into a flow that lands nowhere. The flow itself is [../data-plane/source-flow.md](../data-plane/source-flow.md).

Pick the profile by what you need to survive.

| Need | Profile | Why |
|---|---|---|
| smallest footprint, a stage may drop records when another is down | slim | one pod per stage, direct gRPC, no broker |
| records survive a stage outage on one node | single | one broker holds them |
| records survive outages and the stack scales out | scale | broker cluster, replicated ClickHouse, KEDA |

slim runs on direct, where the receiver pushes records to the loader over gRPC, and to the archiver as well when a source is archived. A source that needs a fetcher or a transform is rejected at save there, so it needs single or scale.

## The bus is Kafka, the broker is a swap

`kafka.mode` picks one broker, an operator-run cluster or an external service, and the provider picks the product.

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
  accTitle: L4 Detail -- Broker choice
  accDescr: kafka.mode single runs one standalone broker. cluster deploys brokers through an operator, Strimzi by default or Redpanda behind its licence gate. external connects to a supplied broker, MSK provisioned, Confluent Cloud or Redpanda Cloud.

  mode{"<b>kafka.mode?</b>"}
  single[("<b>One broker</b><br/><span>StatefulSet</span>")]

  subgraph deployed["Operator-deployed"]
    direction TB
    strimzi[("<b>Strimzi</b><br/><span>default</span>")] ~~~ redpanda[("<b>Redpanda</b><br/><span>licence gate</span>")]
  end

  subgraph supplied["Supplied broker"]
    direction TB
    msk[("<b>MSK</b><br/><span>provisioned</span>")] ~~~ confluent[("<b>Confluent Cloud</b>")] ~~~ rpcloud[("<b>Redpanda Cloud</b>")]
  end

  mode -->|single| single
  mode -->|cluster| deployed
  mode -->|external| supplied
  class single,strimzi,redpanda,msk,confluent,rpcloud bus
  class mode decision
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
  style deployed fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style supplied fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- Broker choice. The Supplied broker group is brokers the deployer supplies.* Up: [Deployment](README.md).

Values in the deploy repo select the broker. `kafka.provider` chooses which operator deploys it in-cluster: Strimzi by default, Redpanda behind a licence gate (`redpanda.acceptLicense`). `kafka.mode: external` with `kafka.external.bootstrap` points the stack at a broker you run, and `kafka.external.provider` names it: `msk`, `confluent-cloud` or `redpanda-cloud` (dfe-infra `helm/charts/kafka/values.yaml`). Deployed brokers and MSK use SCRAM-SHA-512, and Confluent Cloud takes its API key over SASL PLAIN. The apps reject a broker that takes IAM auth alone, such as MSK Serverless (`msk_iam`, scalo-rs `src/transport/kafka/config.rs`). Nothing in the engine changes for any of these. Sizing and the swap matrix: [backing-services.md](backing-services.md).

## Buffering without a bus

On the direct transport no stage stores records, and no fetcher runs, so the receiver is the one stage that holds them. What it holds depends on the receiver's acknowledgement mode.

With acknowledgements on, the default, the receiver builds no buffer or spool. It holds each request until the loader confirms delivery. Once that hold expires the sender gets a 503 and retries from its copy.

With acknowledgements off, the receiver's memory buffer and spool apply. A loader outage shorter than they hold is invisible to senders, and a longer one back-pressures them (503) until the loader returns. A record held there has had its 2xx, so it is lost if the pod dies. That is the trade for turning acknowledgements off.

The knobs are the receiver's (dfe-receiver `config.example.yaml`, `buffer:`), so a deployer sizes them in the overlay like any other dial.
