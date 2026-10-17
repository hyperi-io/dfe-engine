# RBAC - the engine's own reads

Section 5.3 of [rbac.md](rbac.md), which keeps its number so section references resolve.

## 5.3 The Engine's Own Reads

`connections.yaml` names the engine's own ClickHouse connections, and the engine asks for them by name. Only `default` is asked for, by schema discovery. The query API does not lean on a ClickHouse user for tenancy: the executor injects the caller's `org_id` as a reserved bind parameter that a client cannot override (`query/executor.py`, `RESERVED_PARAMS`).

The row reads the engine makes for a caller with no platform grant -- the sampler, the transform dry run, and the schema routes `sample-rows`, `json-paths` and `promote-field` -- bind `_org_id` to the tenant ids of the caller's registered orgs, the same ids each org's pinned user carries (`orgs/tenant_scope.py`). Each org marker the caller carries resolves the same way as a group's (`resolve_orgs`): by name, or failing that by the one org declaring it as a tenant id. A marker no org declares, or a tenant id two orgs share, binds nothing, and a caller left with no tenant id, or a table with no `_org_id`, is refused before any row is read. `/tasks` shows such a caller only the tasks held to its orgs.

The hunt runner's ClickHouse user reads every org, so every statement it compiles carries `_org_id IN (<the hunt's tenant ids>)` as its own conjunct, and a rule condition may read no other table except one in `hunts.lookup_databases`. A listed database holds shared reference data and no org's rows: the engine and the runner refuse to start while one of its tables has an `_org_id` column or reads rows it does not hold, as a view does ([source-detections.md](../data-plane/source-detections.md#lookup-databases)).
