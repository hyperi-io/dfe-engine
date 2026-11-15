# KEDA scaling patterns

Every KEDA-scaled app scales on CPU. The ingest apps also scale on scalo ScalingPressure by default, read through the fail-safe `dfe-keda-shim`. On any shim or ClickHouse failure the last-good value is returned, so scaling freezes at current replicas rather than running up. The deployed charts use no Kedify OTEL Scaler and no Prometheus trigger. The one Prometheus path is `POST /api/v1/pipeline/build`, which renders KEDA `prometheus` triggers against `PROMETHEUS_SERVER_ADDRESS`.

## What DFE scales

| Component | Scales out | Scales in |
|---|---|---|
| DFE apps | KEDA, on CPU and ScalingPressure | KEDA, at the scalo-service scale-down pace |
| Kafka brokers on Strimzi | KEDA, on consumer lag, up to `kafka.autoscaling.maxBrokers` | Off until `kafka.autoscaling.scaleIn.enabled` is set |
| Kafka brokers on Redpanda | By hand | By hand, one broker at a time |
| ClickHouse | Sized, not autoscaled | Sized, not autoscaled |
| Nodes | The platform's node autoscaler | The platform's node autoscaler |

The DFE apps scale out and in through the ScaledObject scalo-service renders for each one. KEDA's `cooldownPeriod` applies only to the step to zero ([ScaledObject spec](https://keda.sh/docs/latest/reference/scaledobject-spec/)), and no DFE tier scales to zero. Between the floor and the ceiling, the HPA that KEDA creates decides.

scalo-service paces that HPA's scale-down. It waits out a 300 second stabilisation window, then removes at most 25 percent of the pods per 60 seconds (scalo-rs `charts/scalo-service/templates/_keda.tpl`). An app's values change it through `keda.scaleDown.stabilizationWindowSeconds`, `keda.scaleDown.percent` and `keda.scaleDown.periodSeconds`. A pod picked to go gets SIGTERM, then up to `terminationGracePeriodSeconds` (45 by default, scalo-rs `charts/scalo-service/templates/_grace.tpl`) to drain before SIGKILL.

On Strimzi, KEDA scales the broker KafkaNodePool on consumer lag in each landing source's transform group, from `kafka.replicas` up to `kafka.autoscaling.maxBrokers` (dfe-infra `helm/charts/kafka/templates/kafka-autoscaler.yaml`). It's on by default and renders once a landing source is declared. A change to the pool's replica count fires Strimzi's Cruise Control autoRebalance, which moves partitions onto a new broker and off a leaving one before Strimzi removes it (`kafka.yaml`, `kafka-rebalance-templates.yaml`).

Broker scale-in is a human decision. Brokers KEDA adds stay until someone sets `kafka.autoscaling.scaleIn.enabled`, which then applies an 1800 second stabilisation window (dfe-infra `helm/charts/kafka/values.yaml`). The [broker autoscaling guide](https://github.com/hyperi-io/dfe-infra/blob/main/docs/deployment/kafka/autoscaling.md) covers the triggers and what has to hold before a scale-in.

The broker autoscaler renders only for `kafka.provider: strimzi`, so Redpanda brokers are sized by hand. Redpanda's own guidance is to keep Kubernetes autoscalers off its brokers ([scaling Redpanda](https://docs.redpanda.com/streaming/current/manage/kubernetes/k-scale-redpanda/)) and to decommission one broker at a time ([decommissioning brokers](https://docs.redpanda.com/streaming/current/manage/kubernetes/k-decommission-brokers/)).

DFE sizes ClickHouse and doesn't autoscale it. The official operator has no autoscaler: scaling is an edit to `spec.replicas` or `spec.shards`, and a new shard takes new writes only, because the operator moves no existing rows ([operator scaling](https://clickhouse.com/docs/products/kubernetes-operator/guides/scaling)).

Nodes are the platform's job. KEDA adds pods, and on AWS Karpenter adds the nodes they need. On-premises DFE ships no node autoscaler, so the node pool is sized for the peak (dfe-infra [autoscaling](https://github.com/hyperi-io/dfe-infra/blob/main/docs/autoscaling.md) and [node autoscaling](https://github.com/hyperi-io/dfe-infra/blob/main/docs/deployment/node-autoscaling.md)).

## The two triggers

**CPU, on every KEDA app.** KEDA's built-in `cpu` scaler (target 70% CPU, metrics-server).

**scalo ScalingPressure, on by default for the ingest apps**: an app's dfe-infra values carry
the `pressure-trigger` trigger fragment, and `keda.pressure.*` sets its target,
shim address and reported service. KEDA takes the higher of it and the CPU
trigger. It is read via the owned, fail-safe `dfe-keda-shim` (rendered by dfe-infra's
`dfe-extras` chart, running the engine container image as `dfe-keda-shim run`) rather than any
Prometheus/Kedify path. The signal is read from the OTel pipeline's
ClickHouse table (`otel_metrics_gauge`). Full fail-safe mechanics:
[hunt-runner-scaling](../data-plane/hunt-runner-scaling.md).

```mermaid
flowchart LR
    ms["metrics-server\n(pod CPU)"] -->|utilization| cpuTrig["cpu trigger\nevery app, 70%"]
    subgraph shimPath["pressure-trigger fragment, ingest apps"]
        direction LR
        gauge[("dfe.otel_metrics_gauge\nscaling_pressure, any prefix")] --> shim["dfe-keda-shim\nGET /keda/pressure?service=X"]
    end
    cpuTrig --> so["ScaledObject\n(per app)"]
    shim -->|"metrics-api\n(fail-safe cache)"| so
    so --> dep["app Deployment"]
```

scalo registers the composite as a bare `scaling_pressure` gauge. An app emits it under that name, or as `<namespace>_scaling_pressure` when its deployment sets a metrics namespace. The shim reads every series of the gauge for one service and returns the hottest:

| Part | Rule |
|---|---|
| Name | The bare `scaling_pressure`, plus any name ending in `_scaling_pressure`, whatever its prefix. A name that ends in `scaling_pressure` without the underscore separator does not match. |
| Service | `ServiceName` is the app instance, matched exactly. |
| Series | One metric name, one attribute set and one pod. A push over OTLP names the pod in the resource (`service.instance.id`), a Prometheus scrape names it in the data-point attributes, and rows that name no pod share one series. |
| Reduction | Each series is averaged over a 120s window, then the MAX across series is returned, so the hottest pod sets the value. The window is two of scalo's 60s export intervals, so every live pod has a reading in it. Averaging the pods together would report a busy pod and an idle one as one lukewarm number. |

With no matching rows the query answers 0. That is a successful answer, so it replaces the cached value rather than holding it, and the CPU trigger sets the replica count. Only a failed query (ClickHouse unreachable, a timeout, a bad query) returns the last-good value, or the `cold_hold` of 0 when there is none yet.

The rule lives in `src/dfe_engine/scaling_pressure.py` and is substituted into both query catalogues (the shim's and the app-management reader's), so the autoscaler and the console match the same rows. The reader matches any prefix, so no app has to rename its gauge.

## Per-app defaults

Read from dfe-infra `argocd/values/apps/<app>/` and `apps.yaml` at the stack the build pins. The profile-slim tier clears the ScalingPressure trigger, so on slim every KEDA app scales on CPU alone.

The docs site fills the table below from the dfe-infra chart renders at the build's own stack, so the published copy carries each app's replica bounds, CPU target and pressure trigger per profile.

<!-- dfe-keda-table -->
| App | KEDA | Triggers by default |
|-----|------|---------------------|
| dfe-receiver / dfe-loader | on | CPU ~70% and scalo ScalingPressure (shim, target 70) |
| dfe-archiver, dfe-transform-elastic / vrl | on | CPU ~70%, plus ScalingPressure on the single, scale and mesh profiles |
| dfe-transform-vector | on | CPU ~70% |
| dfe-ui | on | CPU ~70% |
<!-- /dfe-keda-table -->

Apps that never autoscale:

| App | KEDA | Triggers by default |
|-----|------|---------------------|
| dfe-engine | off - control-plane singleton, always one pod | - |
| dfe-fetcher | off - one pod per fetcher config. The deployer turns KEDA on in that config's values, and only for a source whose fetch supports competing consumers | CPU ~70% once turned on |
| dfe-hunt-runner | off - one static pod | BETA, `huntRunner.autoscaling.enabled`: due-hunt backlog via the shim (`metrics-api`, `AverageValue`). Keep it off: the charts do not materialise the schedule it reads - see [hunt-runner-scaling](../data-plane/hunt-runner-scaling.md) |

Replica bounds come from dfe-infra `argocd/values/common.yaml` and the tier profile:

| Tier | minReplicaCount | maxReplicaCount | Pressure target |
|------|-----------------|-----------------|-----------------|
| common default | 1 | 5 | 70 |
| slim | 1 | 2 | 75 (unused, slim clears the trigger) |
| single | 1 | 3 | 70 |
| scale | 2 | 12 | 70 |
| mesh | 2 | 10 | 70 |

No tier scales to zero.

A per-app `keda.triggers` list is passed through verbatim and replaces the default triggers outright, CPU included. The engine never sets one by default: consumer-group lag also rises when a downstream stage is broken, and more pods cannot help there.
