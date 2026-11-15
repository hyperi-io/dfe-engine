<!-- Project: dfe-engine -->
# DFE GitOps Commit Standard

The governed-ops engine enforces this standard in code (`src/dfe_engine/gitcrud/commit_policy.py`).
It follows the OpenGitOps principles and Argo CD's best practice, amended for DFE's
rule that the repo alone can drive a deployment.

## Scope

This standard governs **machine commits made by dfe-engine** (the governed-ops API
and gitops bridge) to the **gitops deploy/config repos**. It does NOT govern
human source-code commits to dfe-* repos - those follow the hyperi-ai CI standard
(Conventional Commits, `fix:`/`feat:` + semantic-release). Deploy-repo commits are
OPERATIONAL state changes. They never trigger semantic-release.

## 1. Foundational principles (non-negotiable)

The OpenGitOps four, plus our survivability rule:

1. Declarative - desired state is YAML in git, never imperative cluster calls.
2. Versioned + immutable - git history is the SoT. Pin every image/chart by
   tag or SHA. NO `latest`, NO floating refs (immutability principle).
3. Pulled automatically - Argo pulls. The engine never `kubectl apply`s.
4. Continuously reconciled - auto-sync + self-heal on (drift is reverted).
5. Survivable - kill engine + ui and the repo still fully drives + manages DFE.
   Every engine mutation is a git commit, full stop.

## 2. Repository + environment layout

- The deploy repo is separate from the source repos, so access is separate and
  there is no CI->commit->CI loop.
- One deploy repo per deployment. The engine writes `values/` (app-instance
  overlays), `infra/` (cluster and platform overlays), `config/` (sources, schemas,
  hunts, rules, the library) and `governance/` (`src/dfe_engine/gitcrud/registry.py`).
- The tracked branch is `main`. No environment branches: a second environment is a
  second deployment with its own repo.

## 3. Commit conventions (Conventional Commits format, gitops-typed)

Subject: `type(scope): summary` - ASCII only, <= 50 chars, imperative.

- **types** (gitops-operational, NOT release types): `cfg` (config/helm-var change),
  `hunt`, `rbac`, `action` (a defined-action invocation), `ops` (restart/scale/
  rollback), `schema`, `seed`, `lifecycle` (a service set running, paused or stopped).
- **scope** = the resource class + instance, e.g. `cfg(receiver-default)`,
  `hunt(bruteforce)`, `rbac(group:soc-ro)`, `action(scale-receiver)`.
- Body wrap 72. Explains WHY when non-obvious.
- `[skip ci]` on every deploy-repo commit (deploy repo has no semantic-release).
- **Atomic:** one logical change = one commit. A defined-action that touches N vars
  is ONE commit.

Identity + audit trailers (machine-parseable, tie commit <-> SOC2 audit record):

```text
cfg(receiver-default): set replicaCount=3

Scale receiver for ingest backlog.

DFE-Actor: alice@example.com
DFE-Role: helmvars:write
DFE-Action: scale-receiver           # if via a defined action
DFE-Request-Id: 4f3c...              # correlation id, also in the audit log
DFE-Base-Revision: 9c1d...           # optimistic-concurrency base SHA
DFE-Audit-Id: aud_...                # SOC2 audit record id
```

- Author and committer are both the engine's configured git identity
  (`src/dfe_engine/gitops/repo.py` `_commit`). The acting principal is the `DFE-Actor` trailer.

## 4. Branch + PR policy (direct vs PR is POLICY-DRIVEN)

Argo best practice = PRs for prod-controlling branches, enforced at the git level.
We honour it via the RBAC + protected-var policy, not by blocking the engine:

- **Direct-commit mode** (dev / low-risk / non-protected classes): engine commits
  straight to `main` -> Argo auto-syncs. The RBAC check + audit IS the gate.
- **PR mode** (prod env / protected vars / `governance` (RBAC) class / any class
  flagged `require_pr`): engine creates a short-lived branch
  `dfe/<class>/<resource>/<request-id>`, commits, and OPENS A PR via the git
  PROVIDER API (Forgejo/GitHub/GitLab - provider-agnostic. REST via
  AsyncHttpClient, NOT git CLI). A required approval (human or policy bot) merges
  to `main`. Protected-branch rules + approvals are configured at the GIT level.
- Mode is resolved per change from: environment x class x protected-var-policy x a
  `require_pr` flag on the defined action. Recorded in the audit trail.
- Sources, derived schemas, hunts, rules and app overlays all take this path. With
  no forge configured, a production+team write still goes to the branch, never to
  `main`, and takes effect once the branch is merged.
- Short-lived branches are deleted on merge. NO permanent branches besides `main`.

## 5. Signing + verification (supply-chain)

The engine does not sign its commits, and neither the engine nor dfe-infra
configures Argo CD's `sourceIntegrity` verification. Provenance rests on the
`DFE-*` trailers, the audit log and the forge's branch protection.

## 6. Concurrency, rollback, no rewriting

- **Optimistic concurrency** via the commit SHA as the version token (ETag /
  `If-Match`). Stale base -> 409 carrying the current doc and revision, so the
  caller can build a 3-way diff (`src/dfe_engine/gitcrud/engine.py` `ConcurrencyConflictError`).
- **Roll-forward only.** Rollback = `git revert` (or pin the prior tag), NEVER Argo
  rollback (breaks under auto-sync) and NEVER `--force` / history rewrite on `main`
  (Argo would thrash). Reverts are normal commits under this standard.
- Non-fast-forward push (another engine/user pushed first) -> pull + re-evaluate ->
  retry or surface the conflict. Git is the only shared coordination point.

## 7. Self-heal safety (avoid reconciliation loops)

- Do NOT write fields owned by in-cluster controllers under self-heal. KEDA owns `replicas` -> "scale" sets `keda.minReplicaCount`/`keda.maxReplicaCount`, NOT `replicaCount`. Declare `ignoreDifferences` for any dynamic field. Build the exclusion list BEFORE enabling self-heal, not after.
- The exception is a document that explicitly sets `keda.enabled: false`. Nothing owns the count there, and `replicaCount` is the only way to set it. An unset flag is the chart default, which the engine reads off the app manifest (`appmgmt.scaling.keda_by_default`): an overlay in `values/` of a `scale_deployed` app, or one the catalogue cannot place, is read as KEDA-driven, while a platform chart in `infra/` is not unless the document enables KEDA.
- `validate_write` and `validate_revert` (`src/dfe_engine/gitcrud/commit_policy.py`) check the document a write leaves behind and refuse only the violations the write adds, so a document already breaking a rule still takes the write that repairs it.
- dfe-infra's ApplicationSets run auto-sync with self-heal on. Prune is on for the
  layer-2 app, data, edge and platform sets and off for the add-ons and scale sets
  (`argocd/appsets/*.yaml`).

## 8. Enforcement in code (this is a code standard, not just prose)

Implemented in the gitcrud engine:

- `CommitPolicy` builds every message + trailers from the request context (actor,
  role, class, action, request-id, base-SHA, audit-id) - callers cannot hand-write
  messages.
- Mode resolver (direct vs PR) from env x class x policy. PR via the provider API.
- Pre-commit validators: ASCII-only, subject <= 50, type in the allowed set, image/
  chart refs pinned (reject `latest`/floating), no controller-owned fields.

## References

- OpenGitOps Principles (CNCF): <https://github.com/open-gitops/documents/blob/main/PRINCIPLES.md>
- Argo CD Best Practices (env folders not branches, config and source repos split): <https://argo-cd.readthedocs.io/en/stable/user-guide/best_practices/>
- Argo CD Auto-Sync (self-heal, prune, roll-forward): <https://argo-cd.readthedocs.io/en/latest/user-guide/auto_sync/>
- Argo CD anti-patterns (PRs over direct commits, pin bases): <https://codefresh.io/blog/argo-cd-anti-patterns-for-gitops/>
- Argo CD signature verification (GnuPG deprecated -> sourceIntegrity): <https://argo-cd.readthedocs.io/en/latest/user-guide/gpg-verification/>
- Conventional Commits: <https://www.conventionalcommits.org/en/v1.0.0/>
