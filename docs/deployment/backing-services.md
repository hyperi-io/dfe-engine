<!--
  Project:   dfe-engine
  File:      docs/deployment/backing-services.md
  Purpose:   DFE backing services - every backend swaps by config
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# DFE Backing Services - swap every backend by config

DFE is a generic product that ships into clusters we never touch. So every backing
service it stands on - the secrets store, the streaming bus, the object store, the
load balancer, the IdP - has to be swappable without rewriting the product. This
page lists those backing services, the interface and setting each one swaps
through, and which backend fills it per target.

("Backing services" is the Twelve-Factor term: attached resources treated as
swappable, bound by config, never hardcoded.)

## Each backing service swaps on one setting

Every backing service has an interface and a setting, so it swaps
without touching the others or the engine. There is no "on-prem build" or
"cloud build", just a choice per service, and any mix of cells from different
columns is valid.

dfe-infra deploys or wires the backend and injects the config that selects it.
dfe-engine codes to the standard interface and that config, never to a product
by name, and holds no per-cloud branches: an `if aws: ... elif gcp: ...`
belongs in dfe-infra config.

dfe-infra injects the config that picks a backend, and the engine codes to the interface.

```mermaid
flowchart LR
    accTitle: L4 Detail -- Backing-service binding
    accDescr: dfe-infra injects the config that selects a provider. The engine codes to the standard interface and talks to whichever backend the config names.

    cfg["dfe-infra<br/>config"]
    eng["dfe-engine<br/>interface"]
    be[("Concrete<br/>backend")]

    cfg -->|selects| eng
    eng -->|talks to| be

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef store fill:#DDF3EC,stroke:#00785A,stroke-width:1px,color:#0B3327
    classDef ext fill:#EEF0F2,stroke:#6B7280,stroke-width:1px,stroke-dasharray:4 3,color:#1F2937
    class cfg ext
    class be store
```

*Figure 1. L4 Detail -- Backing-service binding.* Up: [Deployment](README.md).

`scalo.secrets` `from_config` is the pattern: the engine reads the provider from config and never names it.

## The matrix

Size and platform are independent. Size sets the composition, and platform sets
which product fills each row of the matrix below.

A deployment picks a size, which sets the composition, and a platform, which sets the product behind each service.

```mermaid
flowchart TB
    accTitle: L4 Detail -- Size and platform axes
    accDescr: Size runs from dfe-docker through slim, single and scale to mesh and sets the composition. Platform runs on-prem, AWS, GCP and Azure and sets which product fills each backing service.

    subgraph size["Size: composition"]
        direction LR
        docker["dfe-docker"] ~~~ slim["slim"] ~~~ single["single"] ~~~ scale["scale"] ~~~ mesh["mesh"]
    end
    subgraph platform["Platform: which product"]
        direction LR
        onprem["On-prem<br/>or Rancher"] ~~~ aws["AWS"] ~~~ gcp["GCP"] ~~~ azure["Azure"]
    end
    size ~~~ platform

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef group fill:none,stroke:#64748B,stroke-width:1px,stroke-dasharray:2 4
    class size,platform group
```

*Figure 2. L4 Detail -- Size and platform axes.* Up: [Deployment](README.md).

Size is the four dfe-infra profiles (`argocd/values/profile-slim|single|scale|mesh.yaml`)
plus `dfe-docker`, which is not Kubernetes. It changes replica counts, single node
against cluster and which optional apps run, never which product a row binds to.
Platform is the swap (`argocd/values/local|rancher|aws|gcp|azure.yaml`).
dfe-infra has OpenTofu environments for `local` and `aws`, and GCP and Azure
have values files but no environment.

Rows are backing services. "Coded to" is the interface the engine or infra uses, and
the platform columns are the concrete backend. A cell names a backend a dfe-infra value,
chart or engine setting selects. A cell marked *(by interface)* codes to the standard
interface, but no value selects it and it is untested.

| Backing service | Coded to | on-prem / Rancher | AWS | GCP | Azure | dfe-docker |
|---|---|---|---|---|---|---|
| Cluster / compute | k8s API | RKE2 / Rancher | EKS + Karpenter | GKE *(by interface)* | AKS *(by interface)* | single host |
| Secrets the engine mints | `scalo.secrets` provider | `file` (default), OpenBao / Vault | Secrets Manager | refused | refused | `file` |
| Data store (CH) | ClickHouse client + `effective_data_database` | DFE CH (single/cluster) | DFE CH, or ClickHouse Cloud as `external` | ClickHouse Cloud as `external` | ClickHouse Cloud as `external` | CH container |
| Streaming | `scalo.kafka` | Strimzi / Redpanda, or a managed Kafka below | MSK provisioned (`msk`, SCRAM-SHA-512), Strimzi, or a managed Kafka below | Strimzi / Redpanda *(by interface)*, or a managed Kafka below | Strimzi / Redpanda *(by interface)*, or a managed Kafka below | Apache Kafka or Redpanda (`KAFKA_BACKEND`), or grpc-direct |
| Object store | S3 API | a supplied S3-compatible store, such as MinIO | S3 (EKS Pod Identity) | GCS *(by interface)* | Blob *(by interface)* | the archiver's local volume |
| Auth / IdP | OIDC, the engine as relying party | any OIDC IdP (`generic`), or local | any OIDC IdP *(by interface)* | Google (`google`) | Entra ID (`entra_id`) | local-auth / oauth2-proxy |
| Ingress / LB | Gateway API / Service | MetalLB + Envoy Gateway | NLB (AWS Load Balancer Controller) + Envoy Gateway | GCLB *(by interface)* | Azure LB *(by interface)* | Envoy (`dfe-proxy`) |
| TLS / PCA | cert-manager Issuer | cert-manager | cert-manager | cert-manager | cert-manager | opt-in, a local CA `make certs` mints |
| GitOps repo | git remote URL | external git, or in-cluster Forgejo | external git, or Forgejo | external git, or Forgejo | external git, or Forgejo | a local deploy repo, or a git server |
| Reconciler (CD) | Argo Application / AppSet | Argo CD | Argo CD | Argo CD | Argo CD | compose render (none) |
| Registry | OCI ref | any OCI registry, `ghcr.io/hyperi-io` by default | same | same | same | same |
| Block storage | StorageClass | local-path, or the cluster default | EBS `gp3` | PD *(by interface)* | Azure Disk *(by interface)* | docker volumes |
| DNS | external-dns / zone | CoreDNS / site DNS | Route 53 *(by interface)* | Cloud DNS *(by interface)* | Azure DNS *(by interface)* | /etc/hosts |
| Observability sink | OTel exporter -> dest | dfe-hyperdx + CH | dfe-hyperdx + CH | dfe-hyperdx + CH | dfe-hyperdx + CH | dfe-hyperdx + CH |

A managed Kafka runs on any platform with `kafka.mode=external`: Confluent Cloud (`confluent-cloud`) and Redpanda Cloud (`redpanda-cloud`), each a provider in scalo's `transport/kafka/providers.rs` with a page under dfe-infra's `docs/deployment/kafka/`. MSK Serverless needs IAM auth. scalo knows it as `msk_iam` and rejects it (scalo-rs `src/transport/kafka/config.rs`), so no DFE app connects to one.

## Worked example - secrets (the pattern for all of them)

The engine builds one manager from config and never names a product. dfe-infra sets
the provider per target, and every row above works the same way.

The engine builds one secrets manager from config, `file` by default, and refuses `gcp` and `azure` at startup.

```mermaid
flowchart LR
    accTitle: L4 Detail -- Secrets provider choice
    accDescr: The engine builds one secrets manager from config. The file provider is the default, openbao or vault and aws select those backends, and gcp or azure are refused at startup.

    eng["dfe-engine<br/>secrets manager"]
    choice{"Provider?"}
    file[("Engine volume<br/>directory")]
    bao[("OpenBao<br/>or Vault")]
    sm[("AWS Secrets<br/>Manager")]
    refused(["Refused at<br/>startup"])

    eng --> choice
    choice -->|file| file
    choice -->|openbao, vault| bao
    choice -->|aws| sm
    choice -.->|gcp, azure| refused

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef store fill:#DDF3EC,stroke:#00785A,stroke-width:1px,color:#0B3327
    classDef decision fill:#FCF1D6,stroke:#9A6700,stroke-width:1px,color:#3D2A00
    classDef error fill:#FBE3D6,stroke:#C2410C,stroke-width:1px,color:#4A1A05
    class choice decision
    class file,bao,sm store
    class refused error
```

*Figure 3. L4 Detail -- Secrets provider choice. The dotted arrow is the refusal.* Up: [Deployment](README.md).

The engine's JWT signing key, ClickHouse passwords and the OIDC client secrets an operator enters all go through `scalo.secrets`. With no `DFE_ENGINE_SECRETS_PROVIDER` they land in the `file` provider under `DFE_ENGINE_SECRETS_PATH` (default `./.secrets`), which is all dfe-docker needs.

`DFE_ENGINE_SECRETS_PROVIDER=openbao` (or `vault`) reads `DFE_ENGINE_SECRETS_ADDR` for the server, `DFE_ENGINE_SECRETS_MOUNT` for the kv mount (default `dfe`) and `DFE_ENGINE_SECRETS_ROLE` for the AppRole role id.

`DFE_ENGINE_SECRETS_PROVIDER=aws` keeps them in AWS Secrets Manager as `<DFE_ENGINE_SECRETS_NAME_PREFIX>/<path>` in `DFE_ENGINE_SECRETS_REGION`, and the engine stops at startup without the region. Each is encrypted with `DFE_ENGINE_SECRETS_KMS_KEY_ID` (the AWS managed key when unset, with a startup warning), tagged with the `DFE_ENGINE_SECRETS_TAGS` JSON object, and deleted with no recovery window unless `DFE_ENGINE_SECRETS_RECOVERY_WINDOW_DAYS` sets 7-30. AWS finishes a forced delete in the background, so re-creating the same name retries for up to 30 seconds. Inside a window the name stays taken: re-creating that OIDC provider answers 409 with the remedy, and a reconcile re-creating that group fails naming the setting.

`gcp` and `azure` are refused: their secret names allow no `/`, and every engine path has one.

dfe-receiver reads its ingest bearer tokens from the same backend, so the backend has to be one the receiver reaches as well. On `openbao` and `aws` it reads them where the engine wrote them, given a policy or role granting `ingest/bearer-tokens/*` and nothing else. The `file` directory sits on the engine's volume, so the deployment mounts `<DFE_ENGINE_SECRETS_PATH>/ingest/bearer-tokens` read-only into the receiver at `/var/run/secrets/dfe/ingest-bearer-tokens`, as a directory so a rewrite reaches it, never the whole secrets root. On Kubernetes no receiver pod can mount that volume. There `DFE_ENGINE_INGEST_BEARER_SECRET` names a Secret the engine writes each listener's lines into as well, one key per listener, through scalo's `kubernetes` provider with `get` and `update` on that one Secret, and the receiver mounts the Secret read-only at the same path.

Who runs each provider: the deployment dial picks a provider per service, injected as
`DFE_ENGINE_DEPLOYMENT_KAFKA_PROVIDER` and `DFE_ENGINE_DEPLOYMENT_CLICKHOUSE_PROVIDER`.
`src/dfe_engine/backing/providers.yaml` says what each provider key means, and
`DFE_ENGINE_PROVIDER_CATALOGUE_FILE` points the engine at another copy. A key the
file does not list reads as DFE-run unless the deploy repo declares
`<prefix>.mode: external`.

| Key under `services.<service>` | Meaning |
|---|---|
| `providers.<key>.managed_by` | `dfe` (DFE runs it), `provider` (a SaaS whose API DFE knows) or `external` (someone else, and DFE has no API to it) |
| `providers.<key>.set_in` | For `provider`: `deployment` (a dial sizes it, change it and redeploy) or `provider` (the provider's console or API) |
| `providers.<key>.read_only` | For `provider`: value paths a write is rejected on with 409 `managed_by_provider`, at the path, below it or above it |
| `external.read_only` | The same for an external service. It leaves out `mode`, because writing `mode` back hands the service to DFE again |

## The dfe-docker special case - kept small by design

dfe-docker is the single-host Compose deployment. It must stay as small as possible: it
is never production, so every backing service takes its lightest backend - no OpenBao,
no ESO, no Argo. Every dfe-docker profile runs dfe-hyperdx.

dfe-docker runs each service in one container on one host, with the file secrets provider.

```mermaid
flowchart LR
    accTitle: L4 Detail -- dfe-docker composition
    accDescr: dfe-docker runs ClickHouse in one container, Redpanda or Apache Kafka on the kafka profiles, receiver and loader, the per-source apps and archiver on single-kafka, dfe-hyperdx with documentdb-local, and the file secrets provider.

    subgraph docker["dfe-docker"]
        ch[("ClickHouse,<br/>one container")]
        broker[("Redpanda or Kafka,<br/>kafka profiles")]
        core["Receiver<br/>and loader"]
        persource["Per-source apps,<br/>single-kafka"]
        observe["dfe-hyperdx and<br/>documentdb-local"]
        secrets[("File<br/>secrets")]
        ch ~~~ core ~~~ observe
        broker ~~~ persource ~~~ secrets
    end

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef store fill:#DDF3EC,stroke:#00785A,stroke-width:1px,color:#0B3327
    classDef async fill:#F6E6EF,stroke:#A8517F,stroke-width:1px,color:#3D1529
    classDef group fill:none,stroke:#64748B,stroke-width:1px,stroke-dasharray:2 4
    class ch,secrets store
    class broker async
    class docker group
```

*Figure 4. L4 Detail -- dfe-docker composition.* Up: [Deployment](README.md).

Levers that keep it small: grpc-direct to skip the bus entirely, single-node
ClickHouse, the `file` secrets provider, one Envoy front proxy (`dfe-proxy`) instead
of a gateway, and TLS when asked for. A heavier backend belongs on the k8s
profiles, not the desktop.
