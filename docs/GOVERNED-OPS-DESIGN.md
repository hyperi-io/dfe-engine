<!-- Project: dfe-engine -->
# Governed Ops + Hunt-Runner - Design & Function Reference

Status: implemented on `feat/governed-ops-api` (not yet pushed/merged). This is the
as-built reference for the new code: what each module does, the key functions, and
how they compose. Companion to docs/ARCHITECTURE.md ("Governed Ops") and the
standards docs (GITOPS-COMMIT-STANDARD, OBSERVABILITY, HYPERDX-FORK-MAINTENANCE).

## 1. The shape (layering)

```
CLI (dfe-api governed ...) ─┐
API routers (helm/governance/config) ─┤── call the SAME services ──┐
                                       │                            v
                              Tier-2: governance/ (actions, policies, ch_rbac, rbac)
                                       │                            │
                              Tier-1 + foundation: gitcrud/ (GitCrud engine)
                                       │                            │
                                       v                            v
                              GitopsRepo (dulwich, no git CLI) -> commit -> Argo reconciles
```

Everything is YAML in git; one generic engine handles every resource class; RBAC is
bound at the class/action/operation level; every mutation ends in a git commit. The
engine never touches the live cluster.

## 2. gitcrud/ - the generic YAML-in-git CRUD engine (the foundation)

- **`models.ResourceClass`** - describes one CRUD class: `name`, `directory`
  (repo-relative), `rbac_prefix`, `suffix`. `.action(verb)` -> the RBAC string,
  e.g. `helmvars:write`.
- **`registry.ResourceClassRegistry`** + **`default_registry()`** - maps a resource
  TYPE name to its ResourceClass. Several types share an `rbac_prefix` (the high-level
  CLASS): `helmvars` (dir `values`); `governance` class = `accounts`/`groups`/`roles`
  (dir `governance/rbac/*`) + `actions` + `policies`. `.classes()` -> the distinct
  RBAC prefixes. (datamodel + hunts classes arrive with the multi-repo work.)
- **`engine.GitCrud`** - the engine. Holds a `GitopsRepo`. Methods:
  - `list(cls)` - enumerate resource names in a class.
  - `get(cls, name)` / `vars(cls, name)` - read the doc / its flattened dot-path vars.
  - `put(cls, name, doc, actor, message=, base_revision=)` - write a whole doc + commit.
  - `set_key` / `delete_key(cls, name, dotpath, ...)` - set/remove one dot-path + commit.
  - `delete(cls, name, actor)` - remove a resource + commit.
  - `put_many(items, actor, message)` - write several resources in ONE commit (atomic
    actions).
  - `head_revision()` / `get_with_revision()` / `_guard_revision()` - optimistic
    concurrency: HEAD SHA is the version token; a stale `base_revision` raises
    `ConcurrencyConflictError(current, head)`.
  - module helpers: `flatten(doc)` (dot-paths), `set_path`/`get_path`.
- **`commit_policy`** - enforces the GitOps Commit Standard in code:
  `CommitContext` + `build_message()` (conforming subject + audit trailers + `[skip ci]`),
  `validate_subject` (ASCII/<=50/allowed type), `validate_change` (reject `latest`/
  unpinned images + controller-owned `replicaCount`), `resolve_mode()` (direct vs PR).
- **`defaults.diff_against_defaults(current_flat, chart_defaults)`** - the
  changed-from-default view for Tier-1 (per-var `default` + `changed`).
- **`factory.build_gitcrud(settings)`** - build a GitCrud from settings, or None when
  gitops is disabled (so the API degrades to 503, startup unaffected).

## 3. governance/ - Tier-2 (curated actions) + policies + RBAC + CH identity

- **`models`** - `VarChange(cls,name,path,value)`, `ActionDef(name, required_action,
  changes)`, `ProtectedPolicy(name, protected[])`.
- **`actions.ActionStore`** - CRUD action defs (gitops `actions` class) + **`invoke()`**:
  applies ALL of an action's changes in ONE commit (`put_many`), honours the
  protected-var policy (a violation aborts the WHOLE action - atomic), `dry_run` returns
  the diff without committing. The curated "big dials" surface.
- **`policies.PolicyStore`** - load protected-var policies; `is_protected(cls,name,path)`
  (fnmatch on `cls:name:path` globs); `enforce(..., override=)` raises `ProtectedVarError`
  unless the caller holds the override grant. The one thing that reaches below a class.
- **`rbac_source`** - load roles/groups FROM gitops (`load_roles`/`load_groups`/
  `resolve_roles_for_groups`) so authz is versioned + survives the engine.
- **`auth_sync.sync_rbac_to_gitops(crud, group_store, role_store)`** - mirror the RBAC
  STRUCTURE (roles+groups) INTO gitops (closes the survivability gap). Accounts/API
  keys hold secret material -> NOT mirrored (ESO follow-up).
- **`ch_rbac`** - the per-group ClickHouse identity (Phase 6): `GroupChBinding`
  (group -> ch_user + grants + settings + quota), `build_group_sql()` (CREATE USER +
  GRANT + SETTINGS PROFILE + QUOTA, backtick-quoted), `ddl_artifact()` (emit as a
  gitops DDL file `ddl/ch-rbac/<group>.sql` - primary path, applied by the migration
  runner like schema DDL), `GroupChProvisioner` (imperative apply - secondary path).

## 4. api/v1/ - the routers (thin; call the services above)

- **`helm`** (Tier-1, `/api/v1/helm`): `GET /files`, `GET /files/{name}/vars`,
  `PUT/DELETE /files/{name}/vars/{path}`. RBAC `helmvars:read|write`(+`override`);
  `If-Match` -> 409 conflict (current vs theirs); commit-policy + protected-var guards;
  audits every mutation.
- **`governance`** (Tier-2, `/api/v1/governance`): list/get actions; `POST
  /actions/{name}/invoke` gated on the action's OWN `required_action` (per-action RBAC,
  resolved at call time); admin CRUD of actions + policies (`governance:write`).
- **`config`** (`/api/v1/config/client`, public, no secrets): runtime config for the UI
  (api base, hyperdx url+enabled, auth mode, feature flags) - fixes the Next.js
  build-time `NEXT_PUBLIC` trap (one image, every env).

## 5. hunt_runner/ - the multi-pod runner (Phase 2)

The pull-based model: hunts are NEVER assigned to pods; workers pull due runs.

- **`models`** - `HuntSpec(hunt_id, interval_seconds, query, target_table,
  timestamp_field="timestamp_load")`, `HuntState(status, overrun_count, too_aggressive)`.
- **`spread`** - deterministic load-spread (no CH thundering herd):
  `phase_offset(hunt_id, interval)` (stable hash offset in `[0, interval*0.8)`),
  `current_fire` (this interval's scheduled fire = boundary+offset), `due_now`
  (has the current fire arrived?), `next_due` (next future fire).
- **`scheduler`** - the never-double-run decision: `decide(state, now, due, running,
  cap)` -> `run` | `defer` (already running) | `wait` (not due / cap reached);
  `mark_deferred` flags `too_aggressive` + bumps overrun.
- **`claim_table.ClaimTable`** (PostgreSQL) - the distribution substrate. DDL for
  `hunt_run` + `hunt_state`; `enqueue`, `claim(worker, now, limit)` via
  **`SELECT ... FOR UPDATE SKIP LOCKED`** (exactly-once across workers), `complete`,
  `reclaim_expired` (crashed-worker lease recovery), `running_count`, `active_hunt_ids`.
- **`checkpoint`** - incremental window on `timestamp_load`: `window(last_watermark,
  scheduled_start, interval)` -> `(start, end)`; `predicate()` -> the SQL clause.
  Crash-safe: the watermark advances only after the query commits.
- **`worker`** - `CheckpointStore` (per-hunt watermark in CH, ReplacingMergeTree),
  `HuntWorker.run(spec, scheduled_start)`: load watermark -> compute window ->
  substitute `{window}` in the rule query -> execute against CH (the query INSERTs
  matched rows into the target table) -> advance the watermark.
- **`runner.HuntRunner`** - ties it together. `enqueue_due(now)` (enqueue this
  interval's fire for any due hunt not already active - deduped), `drain(now)` (claim
  up to `cap - running`, execute each at its scheduled window, complete), `tick(now)`
  = enqueue_due + drain. The daemon is `while: tick(); sleep`.

Live-validated on real PG + CH: concurrent disjoint claims, lease reclaim, full tick
with incremental resume (no duplicate rows).

## 6. alerting/ - scaffold (Phase 7, deferred)

`AlertRule(threshold, window_seconds, severity, destination)` + `fires(rule, count)`;
`AlertDestination`; `Dispatcher(destinations, senders)` with a `Sender` protocol +
`LogSender` placeholder. Real senders (Apprise candidate) + web-research held until the
phase is reached.

## 7. cli/governed_ops.py

`dfe-api governed helm set/get/list` and `action invoke` - a thin wrapper calling the
SAME `GitCrud`/`ActionStore` the routers use (so the gitops-commit path is identical).
For CI / break-glass.

## 8. Principles enforced (acceptance criteria)

- **GitOps-survivability** - every mutation is a `GitopsRepo.publish()`; no live cluster
  client. Kill engine+ui and the repos still drive + manage DFE.
- **RBAC at the abstraction** - bound at class/action/operation handles only, never
  per-var; one `require_action` check per request.
- **CH is the authoritative + safety boundary** - per-group CH user carries grants +
  quota + settings profile (the app layer cannot do quotas/limits).
- **No double-run, ever** - PG row state + lease; overrun -> defer + `too_aggressive`.
- **Crash-safe** - watermark advances only after commit; SKIP-LOCKED leases reclaim.
