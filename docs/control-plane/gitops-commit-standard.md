<!-- Project: dfe-engine -->
# DFE GitOps Commit Standard

The governed-ops engine enforces this standard in code (`src/dfe_engine/gitcrud/commit_policy.py`). It follows the OpenGitOps principles and Argo CD's best practice, amended for DFE's rule that the repo alone can drive a deployment.

It governs the machine commits dfe-engine makes to a deployment's gitops repo, through the governed-ops API and the gitops writer. Human source-code commits to dfe-* repos follow the hyperi-ai CI standard (Conventional Commits, semantic-release). Deploy-repo commits are operational state changes and never trigger a release.

## Principles and layout

The OpenGitOps four, plus our survivability rule:

1. Declarative: desired state is YAML in git, never imperative cluster calls.
2. Versioned and immutable: git history is the source of truth. Pin every image and chart by tag or SHA, with no `latest` and no floating refs.
3. Pulled automatically: Argo CD pulls, and the engine never runs `kubectl apply`.
4. Continuously reconciled: auto-sync and self-heal are on, so drift is reverted.
5. Survivable: with the engine and the UI dead, the repo still drives and manages DFE, because every config change the engine makes is a git commit.

The repository layout:

- The deploy repo is separate from the source repos, so access is separate and there is no CI to commit to CI loop.
- One deploy repo per deployment. The engine writes `values/` (app-instance overlays), `infra/` (cluster and platform overlays), `config/` (sources, schemas, hunts, rules, the library) and `governance/` (`src/dfe_engine/gitcrud/registry.py`).
- The tracked branch is `main` (`DFE_ENGINE_GITOPS_BRANCH`). There are no environment branches: a second environment is a second deployment with a separate repo.

## Commit conventions

The subject is `type(scope): summary`, ASCII, 50 characters at most, imperative. The type is a gitops-operational type, never a release type: `cfg` (a config or helm-var change), `hunt`, `rbac`, `action` (a defined-action invocation), `ops` (restart, scale, rollback), `schema`, `seed` or `lifecycle` (a service set running, paused or stopped). The scope is the resource, for example `cfg(receiver-default-values)`, `hunt(bruteforce)` or `action(receiver-surge)`. When the subject would overrun, the summary is cut first, and the scope is cut too when the type and scope alone pass 50 characters.

The engine writes no prose body. The audit trailers follow the subject, and `[skip ci]` closes every message, because the deploy repo runs no release. One logical change is one commit, so a defined action that touches N vars is one commit. A helm-var write reads:

```text
cfg(receiver-default-values): set maxReplicaCount

DFE-Actor: alice@example.com
DFE-Role: helmvars:write
DFE-Base-Revision: 9c1d...

[skip ci]
```

`DFE-Action`, `DFE-Request-Id` and `DFE-Audit-Id` are added when the write has them. Author and committer are both the engine's configured git identity (`src/dfe_engine/gitops/repo.py` `_commit`), and the acting principal is the `DFE-Actor` trailer.

The gitcrud engine enforces the conventions:

- `build_message` writes every subject and its trailers from the request context (actor, role, action, request id, base SHA, audit id), so callers cannot hand-write a message.
- `resolve_mode` picks direct or PR from the environment, the class, the protected-var policy and the auto-merge setting.
- `validate_subject` rejects a non-ASCII or multi-line subject, one over 50 characters, and a type outside the allowed set. `validate_write` rejects an unpinned image ref (`latest`, or no tag and no `@sha256` digest) and a `replicaCount` KEDA owns.

## Direct commit or PR is a policy decision

Argo CD's best practice is PRs for production-controlling branches, enforced at the git level. We honour it through the environment and the RBAC class rather than by blocking the engine.

- Direct-commit mode (a dev `DFE_ENGINE_ENV`, unprotected vars, any class but `governance`): the engine commits to `main` and Argo CD auto-syncs. The RBAC check and the audit trail are the gate.
- PR mode (a production environment, a protected var, or the `governance` class): the engine commits to a short-lived branch `dfe/<class>/<resource>/<request-id>` and opens a PR through the git provider's REST API, Forgejo, GitHub or GitLab, over scalo's `HttpClient` (`src/dfe_engine/gitcrud/forge.py`). A required approval merges it, and branch protection lives at the git level.
- `resolve_mode` in `commit_policy.py` decides per change. The auto-merge setting (`governance/settings/gitops.yaml`), allowed only on a dev environment or in solo mode, sends everything direct.
- Sources, derived schemas, hunts, rules and app overlays take this path. With no forge configured, a production team-mode write still goes to the branch, never to `main`, and takes effect once the branch is merged.
- Short-lived branches are deleted on merge, and `main` is the one permanent branch.

## Concurrency, rollback and self-heal

- The commit SHA is the version token (`If-Match`). A stale base answers 409 with the current doc and revision, so the caller can build a 3-way diff (`ConcurrencyConflictError` in `src/dfe_engine/gitcrud/engine.py`).
- Roll forward only. A rollback is `git revert` or the prior tag pinned, never an Argo CD rollback, which auto-sync undoes, and never `--force` or a history rewrite on `main`, which makes Argo CD thrash. A revert is a normal commit under this standard.
- A non-fast-forward push (another writer pushed first) pulls, re-evaluates, then retries or surfaces the conflict. Git is the one shared coordination point.
- The engine does not sign its commits, and neither the engine nor dfe-infra configures Argo CD's `sourceIntegrity` verification. Provenance rests on the `DFE-*` trailers, the audit log and the forge's branch protection.

- Never write a field an in-cluster controller owns under self-heal. KEDA owns `replicas`, so a scale sets `keda.minReplicaCount` and `keda.maxReplicaCount`, never `replicaCount`. Declare `ignoreDifferences` for any dynamic field before self-heal is turned on.
- The exception is a document that sets `keda.enabled: false`. Nothing owns the count there, and `replicaCount` is the one way to set it. An unset flag is the chart default, which the engine reads off the app manifest (`keda_by_default` in `src/dfe_engine/appmgmt/scaling.py`): an overlay in `values/` of a `scale_deployed` app, or one the catalogue cannot place, is read as KEDA-driven, while a platform chart in `infra/` is not unless the document enables KEDA.
- `validate_write` and `validate_revert` (`src/dfe_engine/gitcrud/commit_policy.py`) check the document a write leaves behind and reject only the violations the write adds, so a document that breaks a rule still takes the write that repairs it.
- dfe-infra's ApplicationSets run auto-sync with self-heal on. Prune is on for the layer-2 app, data, edge, platform and deploy-repo sets and off for the add-ons and scale sets (`argocd/appsets/*.yaml` in dfe-infra).

## References

- OpenGitOps Principles (CNCF): <https://github.com/open-gitops/documents/blob/main/PRINCIPLES.md>
- Argo CD Best Practices (env folders not branches, config and source repos split): <https://argo-cd.readthedocs.io/en/stable/user-guide/best_practices/>
- Argo CD Auto-Sync (self-heal, prune, roll-forward): <https://argo-cd.readthedocs.io/en/latest/user-guide/auto_sync/>
- Argo CD anti-patterns (PRs over direct commits, pin bases): <https://codefresh.io/blog/argo-cd-anti-patterns-for-gitops/>
- Argo CD signature verification (GnuPG deprecated, sourceIntegrity): <https://argo-cd.readthedocs.io/en/latest/user-guide/gpg-verification/>
- Conventional Commits: <https://www.conventionalcommits.org/en/v1.0.0/>
