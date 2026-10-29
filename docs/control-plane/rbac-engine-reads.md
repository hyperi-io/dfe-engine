# RBAC - the engine's own reads

Section 5.3 of [rbac.md](rbac.md), which keeps its number so section references resolve.

## 5.3 The Engine's Own Reads

The query API does not lean on a ClickHouse user for tenancy: the executor injects the caller's `org_id` as a reserved bind parameter that a client cannot override (`query/executor.py`, `RESERVED_PARAMS`).

The row reads the engine makes for a caller with no platform grant -- the sampler, the transform dry run, and the schema routes `sample-rows`, `json-paths` and `promote-field` -- bind `_org_id` to the tenant ids of the registered orgs a grant holding the read's action reaches (`api/deps.py::held_orgs`), the same ids each org's pinned user carries (`orgs/tenant_scope.py`). A grant bound at an org's scope reaches that org. A tenant role bound system-wide (`org_viewer`, or a `scoped` role) reaches only the orgs its own group names, never one that only another of the caller's groups names, as its group's ClickHouse user does. The sampler, `/tasks`, `sample-rows` and `json-paths` accept that action at system scope or at one of the caller's orgs. `promote-field` changes a source every org shares, so it still needs `schema:write` at system scope. Each org marker the caller carries resolves the same way as a group's (`resolve_orgs`): by name, or failing that by the one org declaring it as a tenant id. A marker no org declares, or a tenant id two orgs share, binds nothing, and a caller left with no tenant id, or a table with no `_org_id`, is refused before any row is read. `/tasks` shows such a caller only the tasks held to its orgs.

`GET /api/v1/sources` and every other source read (`/sources/{name}`, `/columns`, `/flow`, `/signals`, `/export`, `/versions/{version}`, `/catalogue`, `/catalogue-tags`, `/engines`) accept `source:read` at system scope or at one of the caller's orgs. A source definition is shared by every org, so every such caller lists those. A fetcher source belongs to the org it names (the default org when it names none), so a caller without a platform grant lists the fetcher sources of its held orgs only. A named read of a fetcher source bound to an org the caller does not hold gets a 403 `org_not_granted`. A caller with no `source:read` at system scope or in any org gets the 403. The same rule covers `GET /schemas/{source}/columns`, `GET /kafka/topics` (a held caller sees the topics of the sources it may read) and `GET /ingest/rules` (a held caller sees the blocks of those sources). `GET /ingest/compat` and `/ingest/compat-profiles` are the deployment's own bindings and profiles, shared by every source, so any `source:read` holder reads them. `GET /hyperdx/sources` lists every org's HyperDX team, so it needs `source:read` at system scope and refuses a caller held to its orgs.

The hunt runner's ClickHouse user reads every org, so every statement it compiles carries `_org_id IN (<the hunt's tenant ids>)` as its own conjunct, and a rule condition may read no other table except one in `hunts.lookup_databases`. The alert-volume preview a rule write runs carries the same conjunct over the customers of each hunt that runs or names the rule, cut to the orgs the author may read with `rule:write`. A rule no hunt runs is counted over the author's orgs alone, every org for a platform author, and a fence left with no org leaves the rule unmeasured. A listed database holds shared reference data and no org's rows. The runner refuses to start, or stops at its next reload, while one of its tables has an `_org_id` column or is not a plain store of its own rows, as a view is. The engine refuses to start on one too when ClickHouse answers at its start, and starts with the check unrun when it does not ([source-detections.md](../data-plane/source-detections.md#lookup-databases)).

## Usage reads

`GET /api/v1/usage` and `GET /api/v1/usage/overview` read the usage rollup (`usage_daily` and `usage_minute`) and need `usage:read` at system scope or at the scope of one of the caller's orgs ([rbac.md](rbac.md#the-usage-scope)). The deciding step is the same one the other reads use: whether the caller's grant reads every org for `usage:read` (`api/deps.py::reads_every_org`).

A caller whose grant reads every org sees the deployment-wide rows, and may name any registered org with `org`. It can read all four metrics. An unregistered org answers `422 unknown_org`, and an org named for `cores_deployed` or `node_cores` answers `422 not_per_org`, because those two are measured for the deployment.

Any other caller is held to the tenant ids of the registered orgs its groups name (`resolve_orgs`) and grant `usage:read` at. An org where its groups grant only some other role is never read. It reads:

- `ingested_bytes` and `events_per_second` for those orgs added together, or for the one it names with `org`.
- The two core counts as `not_permitted` with no series. Naming one of its own orgs changes nothing for them, and the response is still `200`.

It cannot read the deployment-wide row, the one with an empty `_org_id`, and it cannot read an org it does not hold. Naming one answers `403 org_not_granted`, registered or not. A caller whose groups name no registered org answers `403 no_org`.

Events per second over more than one tenant id is the busiest minute of their sum, taken from `usage_minute`, which keeps 7 days. A held caller with several orgs therefore gets no events-per-second days older than 7. A range with no row reads as `not_measured`, and a day with no row is left out of the series, never 0.
