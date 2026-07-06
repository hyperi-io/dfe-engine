# Anti-pattern analysis (dfe-engine)

Research-backed, functional/operational review (NOT a cyber review). For each
known anti-pattern in the domains this codebase touches, we web-researched the
concrete failure mode + a source, then checked our code, then judged: a
deliberate divergence with a reason (keep) vs a genuine problem (change).

Bar: "not the standard way" is not a finding. A row is only a genuine-problem
when it DEMONSTRABLY bites - a concrete failure mode with a source. Most rows
below are "intentional-and-why": we diverge from the herd on purpose, and the
check confirmed the divergence is sound.

Scope of code checked: `src/dfe_engine/` on `feat/engine-upgrade`. Line numbers
are from that tree at review time.

Deployment fact that colours several rows: the API runs as **single-worker
uvicorn** (`api/__init__.py:69` - one `uvicorn.Server(uvicorn.Config(app))`, no
`workers=`). So one event loop serves all requests; anything that blocks the
loop stalls every other in-flight request.

## Genuine findings (tabled for the operator + Fable review)

| # | Pattern | Where (file:line) | Genuine-problem: failure mode | Keep/Change | Source |
|---|---------|-------------------|-------------------------------|-------------|--------|
| G1 | Blocking sync I/O in `async def` handler (query path) | `api/v1/queries.py:154` `execute_view`, `:207` `execute_raw_query` -> `query/executor.py:121` `self._client.query(...)`, `query/datasources/clickhouse.py:79` `client.query(...)` | These are `async def` but call the SYNC clickhouse-connect client directly, no `await`/offload. On single-worker uvicorn a slow CH query blocks the one event loop, so health checks, UI polls and other queries queue behind it - throughput plateaus and latency balloons under concurrency ("async on the surface, sync underneath"). The team already offloads the git case (`api/v1/gitops.py:99,165` use `await asyncio.to_thread(...)` with a "single-worker uvicorn" comment); the CH query path is the inconsistent gap. | CHANGE - but NOT the naive way (tabled, not applied). See note below. | [FastAPI async docs](https://fastapi.tiangolo.com/async/); [Techbuddies case study](https://www.techbuddies.io/2026/01/10/case-study-fixing-fastapi-event-loop-blocking-in-a-high-traffic-api/) |
| G2 | Trusting inbound identity headers without an in-app gateway trust binding | `api/deps.py:466-520` - `get_current_user` reads `X-Oidc-Subject` / `X-Oidc-Email` / `X-Oidc-Groups` and treats them as an authenticated principal with no credential check | Standard forward-auth pattern, and safe ONLY while two infra invariants hold: (a) the engine pod is never reachable except through Envoy, and (b) Envoy strips/overwrites any client-supplied `X-Oidc-*` before forwarding. If either fails, a client sends `X-Oidc-Subject: admin@corp` + `X-Oidc-Groups: <admin group>` and is authenticated as admin with zero credentials - the exact Traefik/oauth2-proxy CVE class (header spoofing, request smuggling, `_`-vs-`-` normalisation collisions). There is no in-app defence (no shared gateway secret, no mTLS check, no trusted-proxy allowlist). | KEEP the pattern; ADD defence-in-depth (tabled): NetworkPolicy so only Envoy reaches the engine; confirm Envoy strips inbound `X-Oidc-*`; optionally a shared gateway-secret header the engine verifies. Not applied: making a secret mandatory changes the deployment contract and can break the live Envoy integration = risky. | [Traefik ForwardAuth CVE-2026-35051](https://www.systemshardening.com/articles/network/traefik-forwardauth-bypass/); [oauth2-proxy GHSA-7x63-xv5r-3p2x](https://github.com/oauth2-proxy/oauth2-proxy/security/advisories/GHSA-7x63-xv5r-3p2x) |
| G3 | Blocking git/YAML write in the auth path | `api/deps.py:482` `jit.ensure_account(...)` runs synchronously inside `async def get_current_user` | First login (or a group change / stale-login refresh) does a blocking YAML+dulwich write on the event loop, on the request-auth path. Rare (throttled - see K7), so low impact, but it is the same loop-stall class as G1 and is not offloaded. | CHANGE later, low priority - tabled. Same offload constraint as G1 (the account store is shared, so `to_thread` needs the store to be safe under concurrency first). | [FastAPI async docs](https://fastapi.tiangolo.com/async/) |

### Why G1 is tabled, not applied (important)

The obvious fix - drop `async` (FastAPI would offload the `def` to its threadpool)
or wrap the call in `asyncio.to_thread` - is NOT behaviour-preserving here and
would introduce a NEW bug. `ViewExecutor` holds one shared client
(`query/executor.py:91 self._client`) and `ClickHouseAdapter` uses the
process-wide singleton `ClickHouseManager` client
(`query/datasources/clickhouse.py:76`). Today everything is serialised on the
event loop, so that shared client is only ever used one query at a time. The
moment execution runs in threads, two concurrent requests fire two queries on
the same client session and clickhouse-connect raises
`ProgrammingError: Attempt to execute concurrent queries within the same
session. Please use a separate client instance per thread/process.`

So the correct fix is a design change - a per-request client, a unique
`session_id` per query, or `autogenerate_session_id=False` on a dedicated pooled
reader - which is out of scope for a behaviour-preserving cleanup. Recommended
for the operator / Fable review, not applied here.

Source for the constraint:
[clickhouse-connect advanced usage](https://clickhouse.com/docs/integrations/language-clients/python/advanced-usage);
[clickhouse-connect issue #295 (pooling for multi-threaded apps)](https://github.com/ClickHouse/clickhouse-connect/issues/295);
[BetweenSystems: concurrent queries same session](https://betweensystems.com/fixing-concurrent-queries-within-the-same-session-in-clickHouse-async-python-apps/).

## Checked and confirmed sound (intentional-and-why - keep)

| # | Pattern (herd anti-pattern) | Where (file:line) | Intentional-and-why | Keep/Change | Source |
|---|------------------------------|-------------------|---------------------|-------------|--------|
| K1 | Multi-tenant CH: per-tenant user/DB sprawl | `governance/ch/render.py:156` `render_fixed_users`, `:188` `render_tenant_policies`; `governance/ch/models.py:13-17,264`; `connections/tenant.py:11` | We deliberately AVOID per-tenant sprawl. A small fixed set of CH users by privilege + ONE restrictive row policy per `_org_id` table, driven by the per-query `SQL_current_tenant_id` custom setting. This is the production-recommended custom-settings model (comments cite PostHog / Grafana / LaunchDarkly). Per-tenant users destroy connection pooling and hit CH object limits; we sidestep both. Adding the thousandth org adds zero CH objects. | KEEP | [ClickHouse multi-tenancy best practices](https://clickhouse.com/docs/cloud/bestpractices/multi-tenancy); [OneUptime CH multi-tenant schema](https://oneuptime.com/blog/post/2026-03-31-clickhouse-multi-tenant-schema/view) |
| K2 | Row-policy-only with no explicit tenant filter (no PK prune) + fail-open default | `query/executor.py:203` injects `org_id` as a bound view param; `governance/ch/render.py:152,212` empty setting -> 0 rows | Defence in depth: the org filter is a bound parameter on the view AND the restrictive row policy is the backstop, so we are not relying on the policy alone. The tenant setting defaults to empty which fails CLOSED (0 rows), not open, and `AS RESTRICTIVE` is deliberate (a PERMISSIVE policy would flip the table to default-deny for everyone). | KEEP | [ClickHouse multi-tenancy best practices](https://clickhouse.com/docs/cloud/bestpractices/multi-tenancy) |
| K3 | RBAC role explosion / wildcard over-grant / scope confusion | `auth/resources/roles.yaml` | 7 coarse, resource-shaped roles, no user-context encoded in role names, no near-duplicate sprawl. A single global `*` exists on exactly one role (`admin`, line 6) - the correct minimal break-glass use. Everything else is resource-scoped (`hunt:*`, `query:*`) or explicit. `org_analyst` is `scoped: true` + `tenant_scoped: true` for org-restricted reads. | KEEP | [Azure RBAC best practices (avoid wildcards)](https://learn.microsoft.com/en-us/azure/role-based-access-control/best-practices); [Role explosion](https://www.linkedin.com/pulse/role-explosion-unintended-consequence-rbac-oren-ohayon-harel) |
| K4 | Non-idempotent gitops reconcile | `governance/ch/render.py:165-168` (`CREATE USER IF NOT EXISTS` + trailing re-`ALTER ... SETTINGS`), `:208-210` (`CREATE ROW POLICY OR REPLACE`), `:225` (`render_materialise` TRUNCATE + re-INSERT) | Reconcile is idempotent by construction - "make ClickHouse match the config", safe to re-run. They even guard the `IF NOT EXISTS` gotcha (which would skip an edited SETTINGS clause) by re-applying `ALTER USER ... SETTINGS` every run. | KEEP | [OneUptime GitOps anti-patterns](https://oneuptime.com/blog/post/2026-02-26-gitops-anti-patterns/view); [Platform Engineering GitOps patterns](https://platformengineering.org/blog/gitops-architecture-patterns-and-anti-patterns) |
| K5 | Secrets in git (base64 is not encryption) | `governance/ch/render.py:130,176` render `IDENTIFIED WITH sha256_hash BY <hash>` | Only the sha256 hash (a verifier, like `/etc/shadow`) is rendered into DDL; the plaintext is minted via the scalo.secrets seam and never committed. No plaintext credential lands in gitops. | KEEP | [OneUptime GitOps anti-patterns](https://oneuptime.com/blog/post/2026-02-26-gitops-anti-patterns/view) |
| K6 | Read-modify-write / lost-update race on config | `gitcrud/engine.py:52` `ConcurrencyConflictError`, `:322` `_guard_revision`, `:342/:376/:397` put/set_key/delete_key honour `base_revision` | Optimistic concurrency (If-Match): a write carries the base HEAD SHA; if HEAD moved underneath it, the write is rejected with the current doc + head so the caller can 3-way merge. Cross-process, the git push also fails non-fast-forward. Lost-update is handled, not ignored. | KEEP | [OneUptime GitOps anti-patterns](https://oneuptime.com/blog/post/2026-02-26-gitops-anti-patterns/view) |
| K7 | OIDC/JIT per-request write amplification | `auth/jit.py:135-152` `ensure_account` early-return, `:248-250` `_ensure_domain_group` idempotent | JIT does NOT write on every request. An unchanged, recently-active account returns with no write; a write happens only on first login, a group change, or a stale-login refresh past the window. The domain-group is created once and `add_member` writes only for a new member. | KEEP | [OneUptime avoid mutable-default (write-amplification family)](https://oneuptime.com/blog/post/2026-01-25-avoid-mutable-default-arguments-python/view) |
| K8 | OIDC group-claim staleness | `api/deps.py:471-474,497-500` | Header groups are the LIVE per-login IdP signal, UNIONed with DFE-managed store groups. IdP group changes take effect next login rather than being cached stale. This is the correct posture, not staleness. | KEEP | [oauth2-proxy overview (header/claim handling)](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/) |
| K9 | Python mutable default arguments | none found (`rg "def .*=(\[\]\|\{\}\|list()\|dict()\|set())" src` -> 0) | The codebase does not use mutable default args. | KEEP | [Python anti-patterns: mutable default](https://docs.quantifiedcode.com/python-anti-patterns/correctness/mutable_default_value_as_argument.html) |
| K10 | Broad/bare except swallowing errors | 0 bare `except:`; broad `except Exception` are fail-soft with rationale, e.g. `source/registry.py:367,419` (log + skip one bad source YAML), `api/v1/hunts.py:237` (return 0 when the coordination table is absent), `api/v1/gitops` startup guards | No silent swallow in a path that matters: each broad catch is a deliberate fail-open/fail-soft with a WHY-comment and, where relevant, a `logger.warning/exception`. The event these hide is "one bad file / not-yet-provisioned dependency", not an error the caller needs to act on. | KEEP | [Exception-handling anti-patterns](https://medium.com/@jefmoura/avoiding-the-pitfalls-common-anti-patterns-in-exception-handling-in-python-12139e05b6) |
| K11 | FastAPI module-level mutable singleton under multi-worker | `api/deps.py:45` `_registries: dict = {}` | In-process registry cache, populated in the lifespan handler. Harmless under the single-worker deployment (per-process app state is normal FastAPI); would need revisiting only if the deploy ever moved to multi-worker, and even then app state is per-process. | KEEP | [FastAPI async docs (dispatch model)](https://fastapi.tiangolo.com/async/) |

## Applied vs tabled

- APPLIED: nothing. No candidate cleared the bar of clearly-safe AND
  clearly-beneficial AND behaviour-preserving without being churn. The two
  archetypal safe fixes (mutable-default-arg, silent broad-except) do not exist
  in this tree (K9, K10). The one genuine blocking-I/O fix (G1) is NOT
  behaviour-preserving via `to_thread`/`def` because the CH client is a shared
  session and would raise a concurrent-query error - it needs a design change.
  Forcing a change to hit a quota would violate the no-churn / no-risk rule.
- TABLED: G1 (query-path blocking I/O - needs per-request/session CH client),
  G2 (OIDC header trust invariants - infra defence-in-depth), G3 (auth-path git
  write offload - depends on the same store-concurrency work as G1).

## Sources

- ClickHouse multi-tenancy: https://clickhouse.com/docs/cloud/bestpractices/multi-tenancy , https://oneuptime.com/blog/post/2026-03-31-clickhouse-multi-tenant-schema/view
- clickhouse-connect concurrency: https://clickhouse.com/docs/integrations/language-clients/python/advanced-usage , https://github.com/ClickHouse/clickhouse-connect/issues/295 , https://betweensystems.com/fixing-concurrent-queries-within-the-same-session-in-clickHouse-async-python-apps/
- FastAPI async / blocking loop: https://fastapi.tiangolo.com/async/ , https://www.techbuddies.io/2026/01/10/case-study-fixing-fastapi-event-loop-blocking-in-a-high-traffic-api/
- OIDC forward-auth header trust: https://www.systemshardening.com/articles/network/traefik-forwardauth-bypass/ , https://github.com/oauth2-proxy/oauth2-proxy/security/advisories/GHSA-7x63-xv5r-3p2x
- RBAC: https://learn.microsoft.com/en-us/azure/role-based-access-control/best-practices , https://www.linkedin.com/pulse/role-explosion-unintended-consequence-rbac-oren-ohayon-harel
- GitOps: https://oneuptime.com/blog/post/2026-02-26-gitops-anti-patterns/view , https://platformengineering.org/blog/gitops-architecture-patterns-and-anti-patterns
- Python: https://docs.quantifiedcode.com/python-anti-patterns/correctness/mutable_default_value_as_argument.html , https://medium.com/@jefmoura/avoiding-the-pitfalls-common-anti-patterns-in-exception-handling-in-python-12139e05b6
