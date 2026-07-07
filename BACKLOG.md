<!--
  Project:      dfe-engine
  File:         BACKLOG.md
  Purpose:      Durable "too big for now, don't forget" backlog (git-tracked)
  Language:     Markdown
  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# dfe-engine BACKLOG

Durable, git-tracked backlog for work that is DEFERRED but must not be forgotten -
too big for the current release, or blocked on a decision. Distinct from:
- **TODO.md** - near-term, this-release, churns often (ephemeral).
- **STATE.md** - current status + history.

An item graduates OUT of here into a release when it is picked up. Each item
carries: why it is deferred, a rough size, any blocking decision, and a pointer to
the spec/context. Keep it accurate - a stale backlog is worse than none.

## Decisions recorded

- **`POST /queries/raw` hidden from the auto-CLI** (Derek, 2026-07-07, APPROVED).
  Admin-only, non-tenant-isolated arbitrary SQL stays OFF the auto-discovered CLI
  surface (`x-cli:{enabled:false}` in `queries.py`); the HTTP API is unchanged. A
  one-line revert (drop the `openapi_extra`) re-exposes `dfe queries raw` if CLI
  raw-SQL is ever wanted.
- **GitCrud SSoT = the gitops repo** (Derek, 2026-07-07). When the auth/RBAC stores
  route through GitCrud, the gitops repo is the source of truth (every mutation is a
  git commit - the gitops-survivability principle). This unblocks the GitCrud
  converge below and means the dead `auth_sync`/`rbac_source` mirror is WIRED, not
  deleted.

## ClickHouse object (the rebuild's remaining phases)

| Item | Why deferred | Size | Notes / spec |
|---|---|---|---|
| Async CH facade (async client + submit/poll over the same core) | No current caller forces it; the sync path is what everything uses. Fixes the blocking-sync-CH-in-async-handler anti-pattern. | L | Plan `docs/plans/2026-07-06-ch-engine-resolver.md` s12.2 "Async" |
| Allocation-policy quotas (throttle-vs-reject; min across policies; per-tenant concurrency + bytes) | The kill switch already covers the coarse incident-brake case; full per-tenant quotas are a phased scale feature. | L | Plan s12.2 "Allocation-policy quotas" |
| Streaming batch insert / `async_insert` option for high-fanout writers | clickhouse-connect is already streaming-native; no current high-fanout writer needs a bespoke writer. Expose `async_insert`+dedup token when one does. | M | Plan s13.1 |
| `/metrics` scrape endpoint + a metrics backend | The per-query CH metrics are wired + opt-in (`clickhouse.metrics_enabled`) but NoOp until a backend (prometheus_client/OTel) + a scrape endpoint exist. Also unblocks the KEDA prometheus-scaler option. | M | `clickhouse/metrics.py` |

## Governed-ops / gitops

| Item | Why deferred | Size | Notes |
|---|---|---|---|
| GitCrud converge: route the 7 auth/RBAC stores through GitCrud | Big campaign, overlaps the governed-ops-api work. Decision RESOLVED (gitops SSoT, above). | L | `project_governed_ops_api`; wire `governance/auth_sync.py`+`rbac_source.py` |

## Kafka / services config

| Item | Why deferred | Size | Notes |
|---|---|---|---|
| Generate the `default_configs/*-production.yaml` seeds from the plugin templates | The Python side is drift-proof (`production_sasl_scram()`), but the static seed YAMLs are a second hand-maintained copy. Generating them kills the divergence; touches the seed mechanism + deployment contract. | M | 15.7 |
| Consumer-group / client_id SSoT (Pydantic default as the source) | Diffuse across 6-7 sites; lower ROI. | M | 15.7-F2 |

## Dashboards

| Item | Why deferred | Size | Notes |
|---|---|---|---|
| Un-park `overview/alerts` + `overview/hunt_executions` views | Need DFE-owned writer tables (alert results; per-execution records). `hunt_cost_leaderboard` is NOW un-parkable via `dfe_audit.query_log_archive`. | M each | `query/builtin_views/`, `tests/unit/test_query/test_overview_views.py` |

## Observability

| Item | Why deferred | Size | Notes |
|---|---|---|---|
| Distributed tracing / spans | scalo ships metrics but no span/tracer helper; whether app<->CH span joins are worth raw-OTel or a `scalo.tracing` upstream is a someday evaluation. | L | Plan s15.10; `project_observability_standard` |
| Wire `DeploymentContract` validators into CI (`validate_dockerfile`/`validate_helm_values`) | A CI gap, not a duplicate; the chart has app-specific templates beyond `generate_chart()`. | S | Plan s15.10 |

## Reusable Apache-2.0 gems (harvested from the aws-cli/gcloud clones, 2026-07-07)

Derek cloned aws-cli/botocore + gcloud to /Volumes/projects and asked us to lift
battle-tested code (Apache-2.0, attribution required) into the ENGINE, not just the
CLI. These are the vetted candidates - captured here rather than bolted on mid-run
(each is its own tested change; the CLI-relevant ones - `table.py`/`text.py`/
`xform_name` - were already vendored into `cli/auto/vendor/`). Prefer lift-with-
attribution over rewriting (Derek's standing preference).

| Gem (source path) | Applies to | Liftability | Size |
|---|---|---|---|
| gcloud `core/util/retry.py` (`Retryer` + RetryOnResult/RetryOnException, backoff+jitter+predicate) | Task-polling + lifecycle-ready waits (CH DDL/mutation "wait until done", async job state). Richer than a plain HTTP retry - wraps ANY callable with a result-predicate. | LIFTABLE (swap 1 exception import) | M |
| botocore `waiter.py` acceptor matcher (declarative path/status/error success-fail states) | Config-declared lifecycle waits over a polled response (e.g. part-merge count==0, job.state in {DONE,FAILED}). Use gcloud Retryer as the engine + this as the config layer. | PATTERN | M |
| botocore `paginate.py` `TokenEncoder`/`TokenDecoder` (opaque base64+JSON cursor tokens) | CH keyset/cursor pagination - encode the `(sort_key,id)` keyset as an opaque `next_cursor` so clients can't tamper/depend on internals. `query/executor.py` after_key today is a bare literal. | LIFTABLE (stdlib only) | S |
| botocore `exceptions.py` `fmt`-template base (~15 lines: class `fmt`, kwargs, pickle) | Structured, machine-parseable API error bodies with human messages - a clean spine under the existing `ErrorResponse`. | TINY LIFT | S |
| botocore `CredentialResolver` provider-chain (`credentials.py` ~90 lines, dep-free) | Any layered first-hit-wins resolution: secrets source (env->file->openbao->ESO), config-source, connection-target. Complements (not duplicates) scalo's pydantic settings-cascade. | LIFTABLE | M |
| gcloud `core/util/scaled_integer.py` (`ParseInteger("1GiB")`/`FormatBinaryNumber`) | Human byte-size parse/format for query/payload size limits, quota display, ingest batch sizing in configs + API responses. | LIFTABLE (drop `six`) | S |
| botocore `retries/standard.py` `ExponentialBackoff` | ONLY if scalo.http retry does not already honour `Retry-After` + distinguish throttle-vs-transient. Verify against scalo first (scalo-first rule). | CONDITIONAL | S |

Excluded (scalo already covers, or pydantic v2 covers): logging, HTTP client, config
store, CLI base, settings cascade; botocore `validate.py`/`serialize.py` (pydantic).

## CLI (`dfe` auto-generated) follow-ups

The `dfe` CLI (openapi-driven, `src/dfe_engine/cli/auto/`) ships this run. Deferred niceties:

| Item | Why deferred | Size |
|---|---|---|
| `dfe <group> <verb> wait` / `--wait` (poll a returned task to terminal) | Needs the gcloud `retry.py` waiter (above); the task-poll endpoints exist. | M |
| Shell completion (`dfe completion bash/zsh/fish`) | click supports it; generate from the tree. Nice-to-have. | S |
| `--filter`/`--sort-by` client-side on list verbs (gcloud parity) | `--query` (jmespath) covers projection; full gcloud filter DSL is not liftable (coupled). | M |
| Opaque cursor pagination in the CLI | Our REST pagination is page-based today; adopt the `TokenEncoder` cursor gem first. | S |
