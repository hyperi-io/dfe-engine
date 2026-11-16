<!--
  Project:   dfe-engine
  File:      docs/deployment/transports.md
  Purpose:   Choosing a profile and a transport. The bus providers. The mesh balancer
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
| scale out without running a broker | mesh | receiver and loader pools behind per-pool listeners, no fetchers or transforms. In the charts but never run live: dfe-infra's `bootstrap.sh` stops on it ([dfe-infra#875](https://github.com/hyperi-io/dfe-infra/issues/875)) |

slim and mesh run on direct, where the receiver pushes records to the loader over gRPC, and to the archiver as well when a source is archived. A source that needs a fetcher or a transform is rejected at save there, so it needs single or scale.

## The bus is Kafka, the broker is a swap

`kafka.mode` picks one broker, an operator-run cluster or an external service, and the provider picks the product.

```mermaid
flowchart LR
    accTitle: L4 Detail -- Broker choice
    accDescr: kafka.mode single runs one standalone broker. cluster deploys brokers through an operator, Strimzi by default or Redpanda behind its licence gate. external connects to a supplied broker, MSK provisioned, Confluent Cloud or Redpanda Cloud.

    mode{"kafka.mode?"}
    single[("One broker,<br/>StatefulSet")]

    subgraph deployed["Operator-deployed"]
        direction LR
        strimzi[("Strimzi,<br/>default")] ~~~ redpanda[("Redpanda,<br/>licence gate")]
    end

    subgraph supplied["Supplied broker"]
        direction LR
        msk[("MSK<br/>provisioned")] ~~~ confluent[("Confluent<br/>Cloud")] ~~~ rpcloud[("Redpanda<br/>Cloud")]
    end

    mode -->|single| single
    mode -->|cluster| deployed
    mode -->|external| supplied

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef async fill:#F6E6EF,stroke:#A8517F,stroke-width:1px,color:#3D1529
    classDef decision fill:#FCF1D6,stroke:#9A6700,stroke-width:1px,color:#3D2A00
    classDef ext fill:#EEF0F2,stroke:#6B7280,stroke-width:1px,stroke-dasharray:4 3,color:#1F2937
    classDef group fill:none,stroke:#64748B,stroke-width:1px,stroke-dasharray:2 4
    class mode decision
    class single,strimzi,redpanda async
    class msk,confluent,rpcloud ext
    class deployed,supplied group
```

*Figure 1. L4 Detail -- Broker choice. Dashed boxes are brokers the deployer supplies.* Up: [Deployment](README.md).

Values in the deploy repo select the broker. `kafka.provider` chooses which operator deploys it in-cluster: Strimzi by default, Redpanda behind a licence gate (`redpanda.acceptLicense`). `kafka.mode: external` with `kafka.external.bootstrap` points the stack at a broker you run, and `kafka.external.provider` names it: `msk`, `confluent-cloud` or `redpanda-cloud` (dfe-infra `helm/charts/kafka/values.yaml`). Deployed brokers and MSK use SCRAM-SHA-512, and Confluent Cloud takes its API key over SASL PLAIN. The apps reject a broker that takes IAM auth alone, such as MSK Serverless (`msk_iam`, scalo-rs `src/transport/kafka/config.rs`). Nothing in the engine changes for any of these. Sizing and the swap matrix: [backing-services.md](backing-services.md).

## Buffering without a bus

On the direct transport no stage stores records, and no fetcher runs, so the receiver is the one stage that holds them. What it holds depends on the receiver's acknowledgement mode, and dfe-infra's `apps/dfe-receiver/profile-mesh.yaml` sets the figures for both.

With acknowledgements on, the default, the receiver builds no buffer or spool. It holds each request until the loader confirms delivery. Once that hold expires the sender gets a 503 and retries from its copy.

With acknowledgements off, the buffer and spool apply. mesh sets `buffer.memory_limit` to 3 GiB of the pod's 4Gi limit, so back-pressure starts at 2.4 GiB, and turns on `buffer.spillover` with 20 GiB of node-local spool per replica. A loader outage shorter than that is invisible to senders, and a longer one back-pressures them (503) until the loader returns. A record held there has had its 2xx, so it is lost if the pod dies, the trade for turning acknowledgements off.

The knobs are the receiver's (`config.example.yaml`, `buffer:`), so a deployer tunes them in the overlay like any other dial.

## The mesh tier balances between pools with a listener per pool

Status: design built, never run against a live cluster. The charts, the `mesh` profile and the engine's pool addressing are on main, and dfe-infra's `bootstrap.sh` stops on `DFE_PROFILE=mesh` ([dfe-infra#875](https://github.com/hyperi-io/dfe-infra/issues/875)).

Every stage on `mesh` is a pool of replicas. A Kubernetes Service in front of a pool balances per connection, and gRPC holds long connections, so one sender would pin one pod. The profile places a listener in front of each pool. Senders address the listener and every request is balanced. One chart helper renders the same listener for every pool as Gateway API resources (a Gateway listener plus a GRPCRoute), so the balancer is a GatewayClass value: Envoy Gateway by default, because the deployment runs it for its external routes, or a cloud provider's Gateway API class, where the swap is the class name alone.

On `mesh` one listener balances each pool, receivers in front and loaders behind.

```mermaid
flowchart LR
    accTitle: L4 Detail -- Mesh pool listeners
    accDescr: Senders reach the receiver listener, which balances each request across the receiver pool. The receivers push to the loader listener, which balances across the loader pool, and the loaders write the ClickHouse cluster.

    rlisten["Receiver<br/>listener"]

    subgraph rpool["Receiver pool"]
        r1["Receiver 1"]
        r2["Receiver 2"]
    end

    llisten["Loader<br/>listener"]

    subgraph lpool["Loader pool"]
        l1["Loader 1"]
        l2["Loader 2"]
    end

    ch[("ClickHouse<br/>cluster")]

    rlisten --> rpool
    rpool --> llisten
    llisten --> lpool
    lpool --> ch

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef store fill:#DDF3EC,stroke:#00785A,stroke-width:1px,color:#0B3327
    classDef group fill:none,stroke:#64748B,stroke-width:1px,stroke-dasharray:2 4
    class ch store
    class rpool,lpool group
```

*Figure 2. L4 Detail -- Mesh pool listeners.* Up: [Deployment](README.md).

All three transforms have a Push listener, but no transform runs on direct, so none sits in this chain - see [../data-plane/source-flow.md](../data-plane/source-flow.md).

On the direct transport no stage stores records, and no fetcher runs, so the receiver is the one stage that holds them. What it holds depends on the receiver's acknowledgement mode, and dfe-infra's `argocd/values/apps/dfe-receiver/profile-mesh.yaml` sets the mesh figures below.

With acknowledgements on, the default, the receiver builds no buffer or spool. It holds each request until the loader confirms delivery. Once that hold expires the sender gets a 503 and retries from the copy it kept.

With acknowledgements off, the buffer and spool apply. mesh sets `buffer.memory_limit` to 3 GiB of the pod's 4Gi limit, so back-pressure starts at 2.4 GiB, and turns on `buffer.spillover` with 20 GiB of node-local spool per replica. A loader outage shorter than that is invisible to senders, and a longer one back-pressures them (503) until the loader returns. A record held there has had its 2xx, so it is lost if the pod dies. That is the trade for turning acknowledgements off.

The knobs are the receiver's (dfe-receiver `config.example.yaml`, `buffer:`), so a deployer tunes them in the overlay like any other dial.
