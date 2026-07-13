<!-- Project: dfe-engine -->
# DFE GitOps Commit Standard

Status: PROPOSED (feat/governed-ops-api). Strictly adhered to and ENFORCED IN CODE
by the governed-ops engine (not just prose). Aligned to OpenGitOps + current Argo
CD best practice, amended for the DFE gitops-survivability model.

## Scope

This standard governs **machine commits made by dfe-engine** (the governed-ops API
and gitops bridge) to the **gitops deploy/config repos**. It does NOT govern
human source-code commits to dfe-* repos - those follow the hyperi-ai CI standard
(Conventional Commits, `fix:`/`feat:` + semantic-release). Deploy-repo commits are
OPERATIONAL state changes; they never trigger semantic-release.

Web-researched (2026-06-30), see References.

## 1. Foundational principles (non-negotiable)

The OpenGitOps four, plus our survivability rule:
1. **Declarative** - desired state is YAML in git, never imperative cluster calls.
2. **Versioned + immutable** - git history is the SoT; pin every image/chart by
   tag or SHA. NO `latest`, NO floating refs (immutability principle).
3. **Pulled automatically** - Argo pulls; the engine never `kubectl apply`s.
4. **Continuously reconciled** - auto-sync + self-heal on (drift is reverted).
5. **Survivable** - kill engine + ui and the repo still fully drives + manages DFE
   ([[gitops-survivability]]). Every engine mutation is a git commit, full stop.

## 2. Repository + environment layout

- **Config repo is separate from source repos** (already true) - access separation,
  no CI->commit->CI loop.
- **Directory/overlay-per-environment, NOT branch-per-environment.** Permanent env
  branches cause merge hell, drift, and lose the single-SoT view. One config repo,
  a path per env/cluster (e.g. `clusters/<cluster>/...`, `envs/<env>/values/...`),
  a shared `base/` + overlays. Argo's ApplicationSet selects the PATH per
  env/cluster (not a branch). [AMENDMENT to today's `config_repo_revision`-branch
  wiring -> infra alignment task in dfe-infra.]
- **Promotion is a git operation** (PR that bumps the next env's overlay), never an
  edit to a shared file.
- The tracked branch is `main`. No env branches.

## 3. Commit conventions (Conventional-Commits-shaped, gitops-typed)

Subject: `type(scope): summary` - ASCII only, <= 50 chars, imperative.
- **types** (gitops-operational, NOT release types): `cfg` (config/helm-var change),
  `hunt`, `rbac`, `action` (a defined-action invocation), `ops` (restart/scale/
  rollback), `schema`, `seed`.
- **scope** = the resource class + instance, e.g. `cfg(receiver-default)`,
  `hunt(bruteforce)`, `rbac(group:soc-ro)`, `action(scale-receiver)`.
- Body wrap 72; explains WHY when non-obvious.
- `[skip ci]` on every deploy-repo commit (deploy repo has no semantic-release).
- **Atomic:** one logical change = one commit. A defined-action that touches N vars
  is ONE commit.

Identity + audit trailers (machine-parseable, tie commit <-> SOC2 audit record):
```
cfg(receiver-default): set replicaCount=3

Scale receiver for ingest backlog.

DFE-Actor: alice@hyperi.io
DFE-Role: helmvars:write
DFE-Action: scale-receiver           # if via a defined action
DFE-Request-Id: 4f3c...              # correlation id, also in the audit log
DFE-Base-Revision: 9c1d...           # optimistic-concurrency base SHA
DFE-Audit-Id: aud_...                # SOC2 audit record id
```
- **author** = the acting human/principal (blame/attribution).
- **committer** = the `dfe-engine` bot identity (machine provenance). [already split]

## 4. Branch + PR policy (direct vs PR is POLICY-DRIVEN)

Argo best practice = PRs for prod-controlling branches, enforced at the git level.
We honour it via the RBAC + protected-var policy, not by blocking the engine:
- **Direct-commit mode** (dev / low-risk / non-protected classes): engine commits
  straight to `main` -> Argo auto-syncs. The RBAC check + audit IS the gate.
- **PR mode** (prod env / protected vars / `governance` (RBAC) class / any class
  flagged `require_pr`): engine creates a short-lived branch
  `dfe/<class>/<resource>/<request-id>`, commits, and OPENS A PR via the git
  PROVIDER API (Forgejo/GitHub/GitLab - provider-agnostic seam; REST via
  AsyncHttpClient, NOT git CLI). A required approval (human or policy bot) merges
  to `main`; protected-branch rules + approvals are configured at the GIT level.
- Mode is resolved per change from: environment x class x protected-var-policy x a
  `require_pr` flag on the defined action. Recorded in the audit trail.
- Short-lived branches are deleted on merge. NO permanent branches besides `main`.

## 5. Signing + verification (supply-chain)

- **Sign every machine commit** with the `dfe-engine` bot key (provenance + a second
  factor beyond branch protection).
- **Argo verifies via `sourceIntegrity`** (the current mechanism) - NOT the
  DEPRECATED `signatureKeys`/GnuPG project field (slated for removal). Tiered:
  enforce verification on prod, relax on dev.
- Prefer dulwich-native signing to keep the "no git CLI" rule; if dulwich cannot
  sign, signing is the ONE allowed shell-out (to `gpg`/`ssh-keygen`, never `git`).
  [implementation decision - verify dulwich signing support at build time.]

## 6. Concurrency, rollback, no rewriting

- **Optimistic concurrency** via the commit SHA as the version token (ETag /
  `If-Match`). Stale base -> 409 + 3-way diff. [[governed-ops-api]] Task 1.13.
- **Roll-forward only.** Rollback = `git revert` (or pin the prior tag), NEVER Argo
  rollback (breaks under auto-sync) and NEVER `--force` / history rewrite on `main`
  (Argo would thrash). Reverts are normal commits under this standard.
- Non-fast-forward push (another engine/user pushed first) -> pull + re-evaluate ->
  retry or surface the conflict. Git is the only shared coordination point.

## 7. Self-heal safety (avoid reconciliation loops)

- Do NOT write fields owned by in-cluster controllers under self-heal. KEDA owns
  `replicas` -> "scale" sets `keda.min/maxReplicas`, NOT `replicaCount`; declare
  `ignoreDifferences` for any genuinely-dynamic field. Build the exclusion list
  BEFORE enabling self-heal, not after.
- auto-sync + self-heal + prune are ON (prune added gradually). Webhooks configured
  so commits reconcile in seconds, not on the 3-min poll (tightens the
  committed-vs-applied window the API reports).

## 8. Enforcement in code (this is a code standard, not just prose)

Implemented in the gitcrud engine (see [[governed-ops-api]] Phase 1):
- `CommitPolicy` builds every message + trailers from the request context (actor,
  role, class, action, request-id, base-SHA, audit-id) - callers cannot hand-write
  messages.
- Mode resolver (direct vs PR) from env x class x policy; PR via the provider seam.
- Pre-commit validators: ASCII-only, subject <= 50, type in the allowed set, image/
  chart refs pinned (reject `latest`/floating), no controller-owned fields.
- Signing + `sourceIntegrity` config emitted for Argo.
- A CI lint (`dfe local gitops lint`) asserts any deploy-repo history conforms, so
  hand commits (survivability) are checked too.

## References

- OpenGitOps Principles (CNCF): https://github.com/open-gitops/documents/blob/main/PRINCIPLES.md
- Argo CD Best Practices (env folders not branches; config/source repo split): https://argo-cd.readthedocs.io/en/stable/user-guide/best_practices/
- Argo CD Auto-Sync (self-heal, prune, roll-forward): https://argo-cd.readthedocs.io/en/latest/user-guide/auto_sync/
- Argo CD anti-patterns (PRs over direct commits; pin bases): https://codefresh.io/blog/argo-cd-anti-patterns-for-gitops/
- Argo CD signature verification (GnuPG deprecated -> sourceIntegrity): https://argo-cd.readthedocs.io/en/latest/user-guide/gpg-verification/
- hyperi-ai CI standard (Conventional Commits shape we align to): internal CORE.md / GIT.md
