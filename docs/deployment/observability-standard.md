<!-- Project: dfe-engine (applies across dfe-infra, dfe-engine, dfe-ui, rust apps) -->
# DFE Observability Standard

Status: PROPOSED (feat/governed-ops-api). One telemetry contract for the WHOLE
deployment - dfe-infra platform, dfe-engine, dfe-ui, and the Rust apps
(receiver/loader/fetcher/archiver/transforms).

## Principle: ONE OTel destination by default -> HyperDX

Every DFE component sends **logs, metrics, and traces to a single in-cluster
OpenTelemetry Collector** (the gateway). The collector is the ONLY fan-out point;
by default it forwards to **HyperDX ingestion** (so logs land in HyperDX over OTel).
A deployer changes the backend in ONE place, never per-component.

This is the existing telemetry seam, made a hard standard:
`telemetry.mode` = `hyperdx-direct` (default) | `receiver` | `external` |
`prometheus`. Components read the destination from the `dfe-common.otelEndpoint`
helper; nothing talks to HyperDX directly by default.

### Destination modes (ONE config switch)

All components always point at the local collector; the collector's EXPORT target
is the single switch:

| Mode | Path | Lands in |
|---|---|---|
| `hyperdx-direct` (default) | collector -> HyperDX ingest | HyperDX otel CH db (`default.otel_logs`/`otel_traces`/`otel_metrics_*`) |
| `receiver` | collector -> **dfe-receiver** (OTLP) -> kafka -> **dfe-loader** | `dfe.main` (telemetry rides the normal data pipeline) |
| `receiver` + `hyperdx-ingest` | collector -> dfe-receiver (OTLP) -> kafka -> **dfe-loader routes the otel category** | otel CH db (`otel_logs`...) - ingested via DFE, landed in the otel tables so HyperDX still queries it |
| `external` / `prometheus` | collector -> customer OTLP / Prometheus | customer backend |

FACT-CHECKED against the code (2026-06-30):
- **dfe-receiver accepts OTLP** - `otlp-grpc` + `otlp-http` listeners
  (`deployment.rs:80-85`), `otlp` feature + `OtlpConfig` (`config/mod.rs:54`),
  `server/otlp` module. This is the intake for `receiver` mode.
- **dfe-loader is the landing/routing app** - `routing` module with
  category->table mapping ("no table mapping for category"), `parse_db_table`
  (`client_http.rs:192`) lands an arbitrary `db.table`. So the **loader** is the
  app that routes otel-category data to the otel db vs `dfe.main` - it owns the
  `hyperdx-ingest` landing decision. (No otel-category routing rule exists yet;
  that is the work for `hyperdx-ingest` mode.)

Switching is one chart value (`telemetry.mode` [+ `telemetry.landing`]); every
component is unchanged.

## 1. Single destination (the seam)

- `OTEL_EXPORTER_OTLP_ENDPOINT` for EVERY component = the in-cluster otel-collector
  gateway (via `dfe-common.otelEndpoint`). Not HyperDX directly.
- The gateway exports OTLP to HyperDX (+ Prometheus for metrics). Swapping the
  backend is one chart value, not N component changes.
- Already built at the infra layer: the otel-collector daemonset (filelog +
  hostmetrics) -> gateway -> `otlphttp/hyperdx` + `prometheus`. This standard
  extends it to ALL app-level telemetry.

## 2. Logs

- **Apps emit structured JSON to stdout** in ONE cross-language schema:
  `ts` (RFC 3339), `level`, `service.name`, `message`, structured fields,
  `trace_id`/`span_id` when in a trace, with **sensitive-data masked** (PII +
  secrets). The Python reference is the **scalo logger** (`LOG_FORMAT=json`,
  `LOG_OUTPUT=stdout`, built-in masking) - all other languages MATCH its output
  contract (not its code).
- The collector daemonset (`filelog`) scrapes stdout -> gateway -> HyperDX. This
  delivers "logs over OTel" to the single destination WITHOUT coupling each app to
  an OTLP-logging SDK (12-factor; the platform owns routing).
- Optional: direct OTLP log emission for tight trace correlation, for apps that
  already run an OTel SDK.

## 3. Metrics + traces

- OTLP to the gateway. Metrics also exported to Prometheus by the collector (feeds
  KEDA / ScalingPressure). Traces to HyperDX.
- Metric names use the `dfe_*` prefix (per #59 `metric_prefix=dfe`).

### The hunt runner pushes rather than being scraped

The runner has no HTTP listener, so nothing scrapes it. It pushes on the OTLP endpoint the chart already wires and reports `dfe_hunt_runs_total` (`hunt_id`, `outcome`), `dfe_hunt_run_duration_seconds`, `dfe_hunt_rows_written_total`, `dfe_hunt_overruns_total`, `dfe_hunt_claims_total` (`hunt_id`, `outcome`), `dfe_hunt_backlog`, `dfe_hunt_tick_duration_seconds`, `dfe_hunt_detections_capped_total` (`hunt_id`, `rule_id`) and `dfe_hunt_detections_dropped_total` (`hunt_id`, `rule_id`). The backlog gauge runs the SAME due-count SQL the KEDA shim scales on, so a dashboard and the autoscaler cannot disagree about the queue. Each fire also logs one INFO line carrying the hunt, the rows it wrote and how long it took.

A rule compiled into a hunt writes at most `hunts.max_detections_per_run` detection rows per run (default 1000, env `DFE_HUNTS_MAX_DETECTIONS_PER_RUN`), cut to `hunts.max_detections_per_run_ceiling` (default 10000, env `DFE_HUNTS_MAX_DETECTIONS_PER_RUN_CEILING`). A rule that matches more writes the cap plus one summary row: nil `matched_uuid`, `_json` of `{"dfe_capped": true, "matched": N, "written": cap, "rule_id": ...}`, and an `_org_id` only when every match shares one. The run logs one WARN line with the counts, adds 1 to `dfe_hunt_detections_capped_total` and adds the unwritten matches to `dfe_hunt_detections_dropped_total`.

The watermark still advances and the run still records as `completed`, so the same flood is not re-run next fire. A hunt that carries a direct `query` rather than `rules` runs uncapped, and says so in an INFO line each time its specs load.

## 4. Identity

- Every component sets `OTEL_SERVICE_NAME` (`dfe-engine`, `dfe-ui`, `dfe-receiver`,
  ...) + standard resource attributes (deployment env, version) so HyperDX can
  slice by service.

## 5. Defaults

- ON by default in every chart via `dfe-common` helpers (`otelEndpoint`,
  `prometheusAnnotations`). The whole stack is self-observing out of the box, into
  one place.

## 6. dfe-ui / dev-logger (the current gap)

`packages/dev-logger` today: dev-only (no-ops unless `NODE_ENV=development`),
unstructured, no masking, no levels config -> the UI logs NOTHING in production.
Required:
- Replace/upgrade it to emit **structured JSON to stdout in production**, matching
  the scalo log schema (ts/level/service.name/fields, masking, levels). Server-side
  Next.js logs -> stdout -> collector -> HyperDX.
- Honor the OTel env the chart already injects (`OTEL_EXPORTER_OTLP_ENDPOINT`,
  `OTEL_SERVICE_NAME=dfe-ui`). Optionally use the OTel JS SDK for direct OTLP +
  trace correlation; browser-side errors shipped via the server or the JS SDK.

## 7. Acceptance

The CORE 1 self-telemetry e2e test (infra OTel -> HyperDX -> ClickHouse) is
extended so that **engine AND ui** app logs (not just k8s/infra logs) appear in
HyperDX, by `service.name`, within the freshness window - proving every component
reaches the single destination.

## References
- OpenTelemetry Collector (filelog/OTLP routing): https://opentelemetry.io/docs/collector/
- scalo logger (LOGGING.md in the scalo package) - the log-output contract.
- DFE telemetry seam: [[project_deployment_tiers]] (selectable self-monitoring,
  hyperdx-direct default).
