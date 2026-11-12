<!--
  Project:      dfe-engine
  File:         docs/data-plane/sigma.md
  Purpose:      Sigma rule-provider pipeline - feeds, catalogue, propagation, views
  Language:     Markdown
  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# Sigma detection pipeline

Sigma is off by default: every path under `/api/v1/sigma` answers 404 until the engine runs with `sigma.enabled` set, which is `DFE_ENGINE_SIGMA_ENABLED=true` in its environment. A `sigma` entry in a source's `views` is a field-map view and deploys either way.

DFE turns community + commercial Sigma rules into live DFE detections. External
feeds are pulled into an operator-curated catalogue; the operator SELECTS the
rules they want; propagation then generates a DFE detection rule and a per-source
hunt for each selection, running against a per-source Sigma VIEW that exposes the
Sigma-aligned columns. Everything is stored in the deploy repo via gitcrud, so
every change is one attributed commit (survivable + audited).

```mermaid
flowchart LR
    GIT[(SigmaHQ git)] --> P[providers]
    LOCAL[(local dir)] --> P
    VAL[(Valhalla API)] --> P
    P -->|normalise + upsert| CAT[(catalogue<br/>id-keyed gitcrud)]
    CAT -->|operator selects| SEL[selection]
    SEL -->|propagate| RULES[DFE rules + hunts]
    RULES --> HR[hunt runner]
    SRC[source _json] --> VIEW["{source}_sigma view"]
    HR --> VIEW
```

## Providers - pull external feeds

A provider fetches Sigma rules from an external feed and normalises each via
pySigma into a `SigmaRuleDoc` (a mandatory UUID `id` plus `modified`/`date` change
signals, tagged with a provenance `origin`). Three kinds ship:

| Kind | Source | Auth |
|---|---|---|
| `git_repo` | any git repo of `*.yml` (default: `github.com/SigmaHQ/sigma`), cloned via dulwich | git token via the scalo.secrets |
| `local_files` | a directory of `*.yml/*.yaml` under the config dir | none |
| `valhalla` | the Nextron Valhalla JSON feed | api key via the secrets store |

A provider config holds only a secret PATH, never a value. Reach is confined at
register time (`_validate_provider_reach`): a `local_files` directory must resolve
under the config dir; a `git_repo` url / `valhalla` base_url must be http(s), must
not target a loopback/link-local host (the `169.254.169.254` metadata endpoint is
link-local), and - when `DFE_ENGINE_SIGMA_ALLOWED_HOSTS` is set - must be allow-listed.

## Catalogue + selection

`sync_provider` fetches a provider and UPSERTs its docs by `id` into the id-keyed
gitcrud catalogue (`catalog.py`). The upsert is idempotent and local-edit
preserving: an operator can edit a catalogued rule and a later re-sync keeps that
edit (via `deep_merge`, where the operator's lists replace upstream's), reporting drift when the upstream
changed. The operator then SELECTS the rules to activate; selection is its own
gitcrud object.

## Propagation - selection to live detections

`SigmaPropagator.propagate()` walks the selection and, for each selected rule,
generates a DFE detection `Rule` (the Sigma detection converted to a ClickHouse
WHERE over the source's `{source}_sigma` view) plus a per-source hunt. The
generated rule carries a `sigma_rule_id` back-reference and a `sigma_provenance`
marker (the upstream change signal + a hash of the generated WHERE) so drift and
hand-edits are detected. Deselecting a rule leaves its binding STALE until pruned.

The conversion is the [pysigma-backend-clickhouse](https://pypi.org/project/pysigma-backend-clickhouse/) backend, which owns the Sigma modifiers, wildcards and LIKE, regex and CIDR escaping. `sigma_backend_clickhouse.py` subclasses it to emit the bare condition, quote field names through the engine's column quoting and match a text-indexed column through `lower(col) LIKE`. What does not convert is recorded as `failed` in the report:

- A correlation rule, which is a whole query and not a condition.
- A keyword-only rule (`- needle`), which has no column to search. About 3% of the SigmaHQ set.

Plain string equality is case-sensitive, where Sigma matches case-insensitively (`OriginalFileName: CertUtil.exe` misses `certutil.exe`). A JSON-derived view column with no `type` reads as `Nullable(String)`, so `|contains`, `|startswith`, equality and lists work whatever type each row sent, and a missing field is still NULL. Numbers in an equality or a list are compared as quoted text, which suits that column. A `|gt` or `|lt` needs a numeric `type` such as `UInt32`: the view reads it with `toUInt32OrNull`, so a row whose value is not a number reads NULL and matches nothing. Any other declared type reads through `accurateCastOrNull`, with the same NULL for a value that does not convert.

## Source-views - Sigma-aligned columns

A Sigma rule references fields (`Image`, `EventID`) that may not be real columns.
A `SigmaViewDefinition` (`views.py`, one per source) declares each Sigma field as
either a real column OR a path inside the source's `_json` payload, and generates
a `CREATE OR REPLACE VIEW {source}_sigma` that extracts JSON-derived columns with
the `assumeNotNull(_json).path` idiom (as text or a declared type). Identifiers + types
are validated (`_safe_ident` / `_safe_type` / parens-balance) so a definition
cannot inject into the DDL.

## API + RBAC

The `/api/v1/sigma` router exposes provider CRUD + sync, catalogue browse/edit/
adopt, selection, view-definition CRUD + generate, and propagate + bindings.
Reads need `sigma:read`, mutations `sigma:write`, and the URL/path-accepting
provider register/update/delete need `sigma:admin` (admin-only via the `*`
wildcard - the SSRF/path-reach surface). See [rbac.md](../control-plane/rbac.md).

## Query cost attribution + the cost leaderboard

Every ClickHouse query the engine issues carries a JSON `DfeQueryTags` `log_comment`
(service / feature / tenant / kind / id), stamped by the `ClickHouseClientWrapper`
from a request-scoped `tags_context` (feature = the API route) or, for a hunt run,
directly by the worker (feature=hunts, id=the hunt). A materialised view over
`system.query_log` (`clickhouse/query_log_archive.py`) lifts the tagged rows into
`<data database>.query_log_archive`, exploding the JSON into typed columns. dfe-schemas declares the table as optional. `GET
/api/v1/queries/cost-leaderboard` ranks the heaviest cost consumers (query count,
read rows/bytes, duration, peak memory) grouped by the attribution id - the
hunt-cost leaderboard for `feature=hunts`.

## Operator overview dashboards

Six views (`dfe_v_overview_*.sql` in dfe-schemas `views/`) give an at-a-glance
operator picture: active sources, ingest volume by source, pipeline lag (p95),
detections over time, hunt-fleet health, and storage growth. The engine applies
them with the dfe-schemas manifest.
