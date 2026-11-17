# E2E Acceptance - the two scenarios that sign off a change

A change is E2E-verified when it works at BOTH ends of the deployment range: a
full clustered Kubernetes deploy AND a single-host docker deploy. A local dev
hybrid, the engine run against capped local ClickHouse and Kafka, sits between
the two and proves neither, so it never counts as acceptance. It is for fast
unit and integration iteration: a green hybrid run shows the code path works,
not that the product deploys.

DFE is a product suite other organisations deploy. It has to hold up at the
full-cluster end (hosted and customer clusters) and at the single-host end
(dfe-docker).

Acceptance needs both the full Kubernetes deploy and the standalone docker run, and a local hybrid counts as neither.

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
  accTitle: L4 Detail -- Acceptance scenarios
  accDescr: A local dev hybrid is not sufficient for acceptance. The full Kubernetes deploy reads its config and credentials from the deployment's private config and secrets backend, and the standalone docker run feeds what it learns back to dfe-docker. Both must pass.

  cfg[("<b>Private config and secrets</b>")]
  hybrid["<b>Local dev hybrid</b>"]

  subgraph accept["Acceptance, both must pass"]
    direction LR
    k8s["<b>Clustered k8s</b><br/><span>dfe-infra deploy</span>"]
    docker["<b>Single-host docker</b>"]
  end

  dfedocker["<b>dfe-docker</b>"]

  cfg -->|config, creds| k8s
  hybrid -.->|not sufficient| accept
  docker -->|feedback| dfedocker
  class cfg store
  class hybrid,k8s,docker,dfedocker part
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
  style accept fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- Acceptance scenarios. The dotted arrow marks a run that does not count.* Up: [Deployment](README.md).

## The two scenarios

Scenario 1, a full dfe-infra deploy to clustered k8s. What it proves: the product works the way it is operated, with Argo CD
reconciling the GitOps artifacts, backing services, ingress, RBAC and OIDC in
front, on a multi-node cluster.

Where: a multi-node cluster the deployment owns. That deployment's private
config repo holds the cluster topology, endpoints and node ownership.
Credentials come from its secrets backend, never from committed files. On a
shared cluster, deploy onto the nodes and namespace allocated to DFE and
nothing else.

Sign-off: the stack comes up healthy, the UI is reachable, a login works, and a
smoke path through the API and query surface returns. Finish with the deploy
access summary: URLs, how to log in, where to fetch credentials.

Scenario 2, standalone docker. What it proves: the product works on one host with no Kubernetes, the
dfe-docker setup. This is the smallest deployment a first-time adopter stands
up, and what it shows (compose wiring, env, image needs) feeds back into
[dfe-docker](https://github.com/hyperi-io/dfe-docker).

Where: a standalone docker compose bring-up of the engine and its backing
services, pinned by digest the way dfe-docker pins them.

Sign-off: the stack comes up from a clean state, health checks pass, and the
same smoke path through the API and query surface returns. Note anything that
had to differ from the k8s path, because that difference is the feedback for
dfe-docker.

The deployment overview, with its layers, tiers and Argo model, is
[README.md](README.md).
