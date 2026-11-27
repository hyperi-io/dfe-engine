<!--
  Project:      dfe-engine
  File:         docs/data-plane/sigma.md
  Purpose:      Sigma rule-provider pipeline - feeds, catalogue, propagation, views
  Language:     Markdown
  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# Sigma detection pipeline

Sigma is a beta, held back until it is tested from feed to detection. It is off by default behind `sigma.enabled` (`DFE_ENGINE_SIGMA_ENABLED=true`), and while it is off every path under `/api/v1/sigma` answers 404. The work to finish it is tracked in <https://github.com/hyperi-io/dfe-engine/issues/887>.

What works today is the source mapping. A `sigma` entry in a source's `views` is a field-map view, and it deploys whether the flag is on or off. The console does not offer Sigma for a new view, and an existing view keeps it. The rest of the pipeline sits behind the flag, works through the API only with no console flow, and has not yet run from feed to detection.

The design turns community and commercial Sigma rules into DFE detections. External
feeds are pulled into an operator-curated catalogue. The operator SELECTS the
rules they want. Propagation then generates a DFE detection rule and a per-source
hunt for each selection, running against a per-source Sigma VIEW that exposes the
Sigma-aligned columns. Everything is stored in the deploy repo via gitcrud, so
every change is one attributed commit (survivable + audited).

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
  accTitle: L4 Detail -- Sigma detection pipeline
  accDescr: Providers pull rules from SigmaHQ git, a local directory and the Valhalla API, and normalise them into the catalogue. The operator selects rules, and propagation generates DFE rules and hunts, which the hunt runner runs against each source's Sigma view.

  subgraph COLLECT["Collect"]
    direction LR
    GIT[("<b>SigmaHQ git</b>")] --> P["<b>providers</b>"]
    LOCAL[("<b>local dir</b>")] --> P
    VAL[("<b>Valhalla API</b>")] --> P
  end
  subgraph CATALOGUE["Catalogue"]
    CAT[("<b>catalogue</b><br/><span>id-keyed gitcrud</span>")]
  end
  subgraph SELECT["Select"]
    direction LR
    SEL["<b>selection</b>"]
    RULES["<b>DFE rules + hunts</b>"]
    SEL -->|propagate| RULES
  end
  subgraph RUN["Run"]
    direction LR
    HR["<b>hunt runner</b>"] --> VIEW["<b>{source}_sigma view</b>"]
    SRC["<b>source _json</b>"] --> VIEW
  end
  COLLECT -->|normalise + upsert| CATALOGUE
  CATALOGUE -->|operator selects| SELECT
  SELECT --> RUN
  class GIT,VAL ext
  class LOCAL,CAT,SRC,VIEW store
  class P,SEL,RULES,HR part
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
  style COLLECT fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style CATALOGUE fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style SELECT fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style RUN fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- Sigma detection pipeline. Dashed grey boxes are outside DFE.* Up: [Data plane](README.md).

## Providers - pull external feeds

A provider fetches Sigma rules from an external feed and normalises each via
pySigma into a `SigmaRuleDoc` (a mandatory UUID `id` plus `modified`/`date` change
signals, tagged with a provenance `origin`). Three kinds are built, and each works through the API with `sigma.enabled` on. The console has no page for providers or their sync status yet.

| Kind | Source | Auth |
|---|---|---|
| `git_repo` | any git repo of `*.yml` (default: `github.com/SigmaHQ/sigma`), cloned via dulwich | git token via the scalo.secrets |
| `local_files` | a directory of `*.yml/*.yaml` under the config dir | none |
| `valhalla` | the Nextron Valhalla JSON feed | api key via the secrets store |

A provider config holds only a secret PATH, never a value. Reach is confined at
register time (`_validate_provider_reach`): a `local_files` directory must resolve
under the config dir. A `git_repo` url / `valhalla` base_url must be http(s), must
not target a loopback/link-local host (the `169.254.169.254` metadata endpoint is
link-local), and - when `DFE_ENGINE_SIGMA_ALLOWED_HOSTS` is set - must be allow-listed.
A `git_repo` subdir must be a relative path with no `..` (422 `path_escape`), and a
sync reads no file outside the clone and follows no symlink.

## Catalogue + selection

`sync_provider` fetches a provider and UPSERTs its docs by `id` into the id-keyed
gitcrud catalogue (`catalog.py`). The upsert is idempotent and local-edit
preserving: an operator can edit a catalogued rule and a later re-sync keeps that
edit (via `deep_merge`, where the operator's lists replace upstream's), reporting drift when the upstream
changed. The operator then SELECTS the rules to activate. Selection is its own
gitcrud object.

Writing a new rule is not built. `PUT /api/v1/sigma/catalogue/{rule_id}` edits a rule already in the catalogue and answers 404 for a new id, and the console has no catalogue browser or rule editor.

## Propagation - selection to live detections

`SigmaPropagator.propagate()` walks the selection and, for each selected rule,
generates a DFE detection `Rule` (the Sigma detection converted to a ClickHouse
WHERE over the source's `{source}_sigma` view) plus a per-source hunt. The
generated rule carries a `sigma_rule_id` back-reference and a `sigma_provenance`
marker (the upstream change signal + a hash of the generated WHERE) so drift and
hand-edits are detected. Deselecting a rule leaves its binding STALE until pruned.

Propagation runs through the API only. There is no console flow for it, and it has not yet run against real rules and a real source.

The conversion is the [pysigma-backend-clickhouse](https://pypi.org/project/pysigma-backend-clickhouse/) backend, which owns the Sigma modifiers, wildcards and LIKE, regex and CIDR escaping. `sigma_backend_clickhouse.py` subclasses it to emit the bare condition, quote field names through the engine's column quoting and match a text-indexed column through `lower(col) LIKE`. What does not convert is recorded as `failed` in the report:

- A correlation rule, which is a whole query and not a condition.
- A keyword-only rule (`- needle`), which has no column to search.

Plain string equality is case-sensitive, where Sigma matches case-insensitively (`OriginalFileName: CertUtil.exe` misses `certutil.exe`). A JSON-derived view column with no `type` reads as `Nullable(String)`, so `|contains`, `|startswith`, equality and lists work whatever type each row sent, and a missing field is still NULL. Numbers in an equality or a list are compared as quoted text, which suits that column. A `|gt` or `|lt` needs a numeric `type` such as `UInt32`: the view reads it with `toUInt32OrNull`, so a row whose value is not a number reads NULL and matches nothing. Any other declared type reads through `accurateCastOrNull`, with the same NULL for a value that does not convert.

## Source-views - Sigma-aligned columns

A Sigma rule references fields (`Image`, `EventID`) that may not be real columns.
A `SigmaViewDefinition` (`views.py`, one per source) declares each Sigma field as
either a real column OR a path inside the source's `_json` payload, and generates
a `CREATE OR REPLACE VIEW {source}_sigma` that extracts JSON-derived columns with
the `assumeNotNull(_json).path` idiom (as text or a declared type). Identifiers + types
are validated (`_safe_ident` / `_safe_type` / parens-balance) so a definition
cannot inject into the DDL.

There are no stock ECS to Sigma or CIM to Sigma maps yet, so every source maps its own fields.

## API + RBAC

The `/api/v1/sigma` router exposes provider CRUD + sync, catalogue browse/edit/
adopt, selection, view-definition CRUD + generate, and propagate + bindings.
Reads need `sigma:read`, mutations `sigma:write`, and the URL/path-accepting
provider register/update/delete need `sigma:admin` (admin-only via the `*`
wildcard - the SSRF/path-reach surface). See [rbac.md](../control-plane/rbac.md).

## Query cost attribution + the cost leaderboard

This section and the next are not part of Sigma, and they work with `sigma.enabled` off.

Every ClickHouse query the engine issues carries a JSON `DfeQueryTags` `log_comment`
(service / feature / tenant / kind / id), stamped by the `ClickHouseClientWrapper`
from a request-scoped `tags_context` (feature = the API route) or, for a hunt run,
directly by the worker (feature=hunts, id=the hunt). A materialised view over
`system.query_log` (`src/dfe_engine/clickhouse/query_log_archive.py`) lifts the tagged rows into
`<data database>.query_log_archive`, exploding the JSON into typed columns. dfe-schemas declares the table as optional. `GET
/api/v1/queries/cost-leaderboard` ranks the heaviest cost consumers (query count,
read rows/bytes, duration, peak memory) grouped by the attribution id - the
hunt-cost leaderboard for `feature=hunts`.

## Operator overview dashboards

Six views (`dfe_v_overview_*.sql` in dfe-schemas `views/`) give an at-a-glance
operator picture: active sources, ingest volume by source, pipeline lag (p95),
detections over time, hunt-fleet health, and storage growth. The engine applies
them with the dfe-schemas manifest.
