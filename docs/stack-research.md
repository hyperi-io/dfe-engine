# DFE 2.2 Stack Research — dfe-engine Relevance

> **WARNING: THIS DOCUMENT WILL DRIFT**
>
> Extracted from dfe-infra research corpus on 2026-03-31. Stack decisions
> are still being finalised as dfe-infra ports from 2.1 to 2.2. Cross-check
> against `/projects/dfe-infra/docs/` and `versions.yaml` for current state.

## Auth: Envoy Gateway OIDC

**Decision:** Envoy Gateway replaces nginx-ingress (EOL March 2026) + oauth2-proxy + Dex.

**How it works:**
- Envoy Gateway `SecurityPolicy` CRD configures OIDC per-route
- Authorization code flow → session cookie → forward headers
- Headers injected: `X-Oidc-Subject`, `X-Oidc-Groups`, `X-Forwarded-User`
- Any OIDC provider: Entra ID, Google Workspace, Cognito, Keycloak

**Impact on dfe-engine:**
- `get_current_user()` needs a dual-mode path: OIDC headers (Envoy-fronted) + JWT Bearer (API clients)
- Group-to-role mapping: OIDC groups from `X-Oidc-Groups` header → DFE roles
- `generate_rbac_csv()` already exists — needs to consume OIDC group mappings
- LocalAuthProvider stays as dev/test fallback
- Standalone/Docker auth mode (no Envoy) still being designed — JWT Bearer only

**Spike code:** `docs/oauth2/spike/envoy-gateway.yaml` has a working SecurityPolicy example.

## Observability: OTel → ClickHouse → HyperDX

**Decision:** Replace Prometheus + Grafana with unified OTel pipeline.

**Architecture:**
```
DFE services (Rust: hyperi-rustlib metrics, Python: hyperi-pylib logger)
    → OTel Collector Gateway (OTLP gRPC/HTTP)
        → ClickHouse (analytics + observability store)
        → HyperDX (UI, search, dashboards)
```

**Impact on dfe-engine:**
- `hyperi-pylib.logger` already auto-emits structured logs (RFC 3339 JSON in containers)
- OTel auto-instrumentation for FastAPI (`opentelemetry-instrumentation-fastapi`) to be added
- Helm compiler already injects OTEL config into service values
- No Prometheus endpoint needed — OTel Collector handles everything

## Scaling: KEDA from OTel Metrics

**Decision:** KEDA reads scaling signals from OTel metrics, not Prometheus scrape.

**Primary scaler:** Kedify OTEL Scaler (direct OTLP push)
**Fallback:** Prometheus trigger against OTel Collector's `:8889` endpoint

**Rust service metric:** `dfe_scaling_pressure` (composite 0-1 gauge)
**Transform services:** Scale-to-zero via Kafka consumer lag

**Impact on dfe-engine:**
- `DeploymentConfigRegistry` already models KEDA config (ScaledObject, triggers)
- Helm compiler already generates KEDA values
- dfe-engine doesn't emit `dfe_scaling_pressure` (that's Rust) — it configures the thresholds
- Scale-to-zero for transforms: each transform is tied to a single source Kafka topic

## Secrets: External Secrets Operator

**Decision:** ESO bridges cloud-specific secret stores → K8s Secrets.

| Environment | Backend |
|-------------|---------|
| Local/DevEx | OpenBao (Vault fork) |
| AWS | AWS Secrets Manager |
| GCP | GCP Secret Manager |
| Azure | Azure Key Vault |

**Impact on dfe-engine:**
- Secrets arrive as K8s Secrets → mounted as env vars or files
- dfe-engine reads via `DFESettings` (Pydantic settings with env var binding)
- No direct Vault/SM client needed in dfe-engine
- Chart already supports `existingSecret` overrides

## Kafka: Strimzi (KRaft, no ZooKeeper)

**Decision:** Strimzi operator manages Kafka clusters in KRaft mode (no ZooKeeper).

**Version:** Kafka 3.9.0, Strimzi 0.50.1
**Auth:** SASL/SCRAM-SHA-512

**Impact on dfe-engine:**
- `ServiceConfigRegistry` already models Kafka config for Rust services
- Helm compiler injects `config.kafka.bootstrap_servers`, `config.kafka.sasl.*`
- dfe-engine doesn't connect to Kafka directly — Rust services do

## ClickHouse: Altinity Operator

**Decision:** Altinity ClickHouse Operator manages ClickHouse clusters.

**Version:** ClickHouse 24.8, Operator 0.23.0

**Impact on dfe-engine:**
- `clickhouse-connect>=0.15.0` client library
- Schema module generates DDL for ClickHouse tables
- HuntEngine executes queries against ClickHouse
- Helm compiler injects `config.clickhouse.host`, `config.clickhouse.port`

## PostgreSQL: CNPG (CloudNativePG)

**Decision:** CNPG operator for PostgreSQL 17. Used by UI and HyperDX only.

**Impact on dfe-engine:** None. dfe-engine uses YAML SSoT, not PostgreSQL.

## Container Registry: JFrog

**Decision:** JFrog Artifactory for all container images + Helm charts.

**Impact on dfe-engine:**
- Docker image built by hyperi-ci, pushed to JFrog
- Helm chart published to JFrog
- `imagePullSecrets` created by bootstrap.sh in each namespace

## Naming Standard: tf-naming

**Canonical format:** `{project}-{component}-{env}` (max 30 chars)

Derives all platform-specific names (K8s namespace, service account, IRSA role,
GCP SA, Vault path, etc.) from four dimensions: project, component, env, cloud.

**Impact on dfe-engine:** Helm compiler should respect this naming convention
when generating Argo CD app names and namespace references.

## Version Pinning: versions.yaml

All component versions pinned in `/projects/dfe-infra/versions.yaml` (SSOT).
Every chart, operator, and bootstrap script reads from this file.

dfe-engine version is tracked there as `apps.dfe-engine`.
