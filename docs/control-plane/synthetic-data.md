# Synthetic Data

Realistic synthetic events on demand, for two jobs: a continuous live-looking inbound stream for sales demos, and repeatable data for testing. It is never a load generator: every knob is a ceiling, and the shipped defaults generate nothing until an operator or caller asks.

Every synthetic event has a marker (overridable per request), which the common header lands in `_tags`, so synthetic data is always identifiable downstream. Where the pack's `tags` is an object, the event has `tags.synthetic: true`, found as `_tags.synthetic`. Where it is an ECS keyword list (`meta/elastic/ecs` and the beats packs that map ECS `tags`), the event has the keyword `synthetic` in that list, found with ``has(_tags.list.:`Array(Nullable(String))`, 'synthetic')``, and extra `tags` in the request are rejected because a list cannot hold keys (`src/dfe_engine/synthetic_data/tags.py`).

Schema packs and sampled rows feed two event factories, and a paced stream posts their events to dfe-receiver.

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
flowchart TB
  accTitle: L4 Detail -- Synthetic data paths
  accDescr: dfe-schemas meta packs feed the schema event factory, and sampler rows or lines feed the sample event factory, which scrubs identities. Both feed a Poisson-paced stream that posts events to dfe-receiver's ingest endpoint.

  subgraph inputs["Inputs"]
    direction LR
    packs[("<b>dfe-schemas</b><br/><span>meta packs</span>")]
    sample[("<b>Sampler rows or lines</b>")]
  end

  subgraph factories["Factories"]
    direction LR
    schemaf["<b>SchemaEventFactory</b>"]
    samplef["<b>SampleEventFactory</b><br/><span>identity scrub</span>"]
  end

  stream["<b>Poisson-paced stream</b>"]

  receiver["<b>dfe-receiver</b><br/><span>ingest</span>"]

  packs --> schemaf
  sample --> samplef
  schemaf --> stream
  samplef --> stream
  stream --> receiver
  class receiver ingest
  class packs,sample store
  class schemaf,samplef,stream part
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
  style inputs fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style factories fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- Synthetic data paths. dfe-receiver is outside the engine.* Up: [The DFE control-plane layer](README.md).

From the receiver, a synthetic event takes the same path as any other: the loader writes it to ClickHouse.

| Mode | Input | Realism source |
|------|-------|----------------|
| Reference packs (default) | A dfe-schemas meta schema (`meta/syslog`, `meta/otel/logs`, `meta/beats/filebeat`, the cyber sets) | `@source:` expressions give the event fields, name and type heuristics pick typed generators, and schema-authored `synthetic:` hints and scenarios hold vocabularies and message templates |
| Lookalike | A sample's parsed `rows` or raw `lines` (the sampler's output) | The sample's distributions: enums replay weighted, numerics fit the observed range, free text keeps its structure with identity tokens substituted in place |

Both draw from a seeded entity pool: a generated host keeps its FQDN, IP, MAC and users across the whole stream, and an identical seed reproduces an identical stream. Lookalike mode never replays sample identities. IPs, MACs, UUIDs, emails and name-identified user and host keys are synthesised, so it doubles as a scrub.

Correlated columns must not draw independently (an sshd message under `appname: cron` reads as fake). A pack's version entry may declare `synthetic.scenarios`: one weighted scenario is drawn per event and pins the correlated columns together. The format is in dfe-schemas' [meta-schema.md](https://github.com/hyperi-io/dfe-schemas/blob/main/docs/meta-schema.md).

## API and limits

`GET /synthetic-data/packs`, `POST /synthetic-data/generate`, `POST /synthetic-data/lookalike`, and the stream task endpoints (`POST /synthetic-data/stream`, `GET /synthetic-data/streams`, `GET /synthetic-data/streams/{operation}`, `POST /synthetic-data/streams/{operation}/cancel`). Reads need `synthetic-data:read`. Generating or streaming needs `synthetic-data:run`, write-grade because it injects data into the pipeline. The full contract is `openapi/openapi.json`.

`SyntheticDataSettings` (`DFE_ENGINE_SYNTHETIC_DATA_*`) caps inline counts, stream rate and stream duration. The autostart settings run a standing sales demo: name the pack refs and a receiver URL (`DFE_ENGINE_SYNTHETIC_DATA_AUTOSTART_*`, set through the overlay's `extraEnv`) and the engine keeps those streams running for the pod's lifetime, in bounded segments, retrying when the receiver is away, cancelled at shutdown. Empty, the default, is off.

## Receiver auth

A stream over https to the deployment's own receiver presents the engine's own ingest credential, `dfe-engine-synthetic-data`, owned by and binding the primary org on the HTTP listener, so its records land in that org. The own receiver is the host of `synthetic_data.autostart_receiver_url` or one listed in `synthetic_data.allowed_receiver_hosts`. A stream to any other URL, or over plain http, carries no engine credential, so a caller can't have the token sent to a host it picked. The engine mints it on the first such stream once a primary org is named, and again when its token is gone or the primary org changes. It never turns the listener's auth on, so on a listener running `mode: none` its token is ignored and records land under the receiver's `default_org_id`, your org's first tenant id. The receiver reads a new token within 60 seconds, and a stream retries a 401 for 90 seconds after a mint. A request whose `headers` carry its own `Authorization` presents that instead. No org can create a credential with this id (409 `credential_id_taken`).
