<!-- Project: dfe-engine (governs the hyperi-hyperdx fork) -->
# dfe-hyperdx Fork Maintenance Standard

Status: PROPOSED. How we carry DFE changes on the hyperi-hyperdx fork with the
MINIMUM ongoing cost of tracking upstream HyperDX.

## The principle

Fork maintenance cost is paid almost entirely in the upstream files we
edit (the fork surface). Drive that to ~zero: keep all DFE code additive and namespaced, feed
upstream's own extension points instead of reimplementing its core, and track
upstream by tag with a rebase + CI guardrail. Authority stays in the engine/gitops
([[gitops-survivability]]); the fork only APPLIES decisions.

## Rules

1. **Additive-only, `dfe/`-namespaced.** All custom code lives under
   `packages/api/src/dfe/` (and equivalents). Never edit upstream files except
   unavoidable registration points.
2. **Feed extension points; do not fork core.** Our code POPULATES upstream
   mechanisms - e.g. the connection selector SETS the native
   `x-hyperdx-connection-id` header from the user's group, so upstream's existing
   connection-selection runs UNCHANGED (zero edits to `clickhouseProxy`/connection
   model). Same pattern everywhere: drive native behaviour, don't reimplement it.
3. **Count + document the fork surface.** Maintain `.fork-surface` listing every upstream
   file we edit (target: middleware registration only). A bump review is then
   "check those files still match", nothing more.
4. **Track upstream by RELEASE TAG, not main.** Bump deliberately under the DFE
   supply-chain policy (>=7-day cooldown, prefer the LTS line). Pin the tag.
5. **Rebase the DFE commit set onto each new tag.** Keep the delta a small, ordered
   commit set (ideally: added files + one registration patch). Isolation -> conflicts only
   in the fork surface; the DFE delta stays visible as the top commits.
6. **CI guardrail on every bump.** Automate: rebase onto the new tag -> build -> run
   a DFE fork-surface integration suite (assert: dfe middleware registered; OIDC group
   headers honoured; group->connection selection works; engine JWT verified). A broken
   registration fails CI immediately, not in production.
7. **Lock-step the fork release with dfe-ui.** One coordinated bump; the fork image
   and dfe-ui move together.
8. **Config over code.** Behaviour is driven by engine-provided config (the
   group->connection map, auth header names, env) - so upstream bumps that do not
   touch the fork surface need NO fork code change, only a rebase.
9. **Trim the delta.** Audit the existing fork (known to carry excess) down to the
   minimal fork surface. Every removed line is maintenance saved; smaller delta = cheaper
   rebases.
10. **Upstream the extension POINT where feasible.** If HyperDX will accept a generic
    hook (e.g. a connection-resolver), contribute it; our delta then shrinks to
    config and that edit disappears.

## What our delta SHOULD be (target shape)

- Added: `packages/api/src/dfe/**` (oidc-identity, jwt-verify, user-provisioning,
  config, connection-selector) - self-contained.
- Upstream edit: ONE middleware-registration line wiring `dfe/` into the app pipeline.
- NOT touched: query engine, connection model/storage, `clickhouseProxy` core,
  upstream auth - all driven via headers/config from the `dfe/` layer.

## References

- Git rebase-onto-tag workflow for forks (isolate delta, rebase per release).
- DFE supply-chain cooldown/LTS: standards/universal/SECURITY.md.
- Design that this serves: [[project_hyperdx_ch_rbac]] (group->CH connection).
