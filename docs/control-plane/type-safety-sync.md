# Automagic type safety (dfe-engine → dfe-ui)

dfe-engine is the source of truth for the HTTP API contract and the RBAC scope catalog. Generated artifacts in this repo are synced into **dfe-ui** so the frontend can use strict TypeScript types and scope constants without hand-maintaining duplicates.

## Goal

- **API types**: Routes, request bodies, and responses defined in FastAPI/Pydantic become TypeScript types in dfe-ui.
- **RBAC scopes**: Permission strings in `scope_constants.py` become a typed `scopes` object and `RbacScope` union in dfe-ui.
- **Automation**: Merges to `main`/`master` that touch the spec or scope catalog open a PR in dfe-ui; frontend code can rely on compile-time checks against the latest engine contract.

## Generators in this repo

### OpenAPI spec

| Item | Location |
|------|----------|
| Script | `openapi-spec/generate.py` |
| Output | `openapi-spec/openapi.json` and `openapi-spec/openapi.e2e.json` (committed) |

Regenerate after API model or route changes:

```bash
uv run python openapi-spec/generate.py
```

The script loads the live FastAPI app and writes the OpenAPI 3 schema. That JSON is used for Prism mocks, contract checks, and (in dfe-ui) `openapi-typescript` code generation.

See also [UI-API-GUIDE.md](ui-api-guide.md) for local mock/typegen workflows.

### RBAC scope constants

| Item | Location |
|------|----------|
| Source of truth | `src/dfe_engine/auth/rbac_scopes/scope_constants.py` (`*_scopes` dict literals) |
| Script | `rbac-scopes-spec/generate.py` |
| Output | `rbac-scopes-spec/scopes/index.ts` (committed for review) |

Regenerate after adding or renaming scopes:

```bash
python rbac-scopes-spec/generate.py
# or
uv run python rbac-scopes-spec/generate.py
```

The generator parses `scope_constants.py` with the AST (no import side effects), merges all `*_scopes` dicts into `export const scopes`, and emits `export type RbacScope`.

## CI sync to dfe-ui

Workflow: [`.github/workflows/sync-dfe-engine-types.yml`](../../.github/workflows/sync-dfe-engine-types.yml)  
**Name:** `Sync dfe-engine-types`

### Triggers

- **Push** to `main` or `master` when any of these paths change:
  - `openapi-spec/openapi.json`
  - `openapi-spec/openapi.e2e.json`
  - `src/dfe_engine/auth/rbac_scopes/scope_constants.py`
- **Manual:** `workflow_dispatch`

Note: Changing only Python API files without regenerating and committing the OpenAPI JSON will not trigger the workflow. Regenerate locally with `uv run python openapi-spec/generate.py` and commit `openapi-spec/openapi.json` and/or `openapi-spec/openapi.e2e.json` when the contract changes.

### What the job does

1. Checks out dfe-engine.
2. Runs `python3 rbac-scopes-spec/generate.py` (fresh `scopes/index.ts` from current `scope_constants.py`).
3. Clones **dfe-ui** (repo from `DFE_UI_REPO`, default `hyperi-io/dfe-ui`) using `DFE_UI_TOKEN`.
4. Copies into dfe-ui’s types package:
   - `openapi-spec/openapi.json` → `packages/dfe-engine-types/specs/openapi.json`
   - `openapi-spec/openapi.e2e.json` → `packages/dfe-engine-types/specs/openapi.e2e.json`
   - `rbac-scopes-spec/scopes/index.ts` → `packages/dfe-engine-types/scopes/index.ts`
5. Runs `yarn install --immutable` and `yarn generate` in `packages/dfe-engine-types`, then generates `types/e2e.d.ts` from `openapi.e2e.json` and exports it as `@repo/dfe-engine-types/e2e`.
6. Opens a PR in dfe-ui (closes older open sync PRs on the same branch naming pattern so only one sync PR is active).

### Secrets and configuration

- **`DFE_UI_TOKEN`** (required): PAT with permission to push branches and open/close PRs in dfe-ui.
- **`DFE_UI_REPO`** (optional): `owner/name` for the UI repo (default `hyperi-io/dfe-ui`).

## Developer workflow (dfe-engine)

1. Change API or scopes in Python.
2. Regenerate artifacts:
   - API: `uv run python openapi-spec/generate.py`
   - Scopes: `uv run python rbac-scopes-spec/generate.py`
3. Commit the updated `openapi-spec/openapi.json`, `openapi-spec/openapi.e2e.json`, and/or `rbac-scopes-spec/scopes/index.ts` together with your code change.
4. After merge to `main`/`master`, wait for **Sync dfe-engine-types** to open the dfe-ui PR; merge that PR to pick up types and scopes on the frontend.

## Developer workflow (dfe-ui)

Consume **`packages/dfe-engine-types`** (generated API types and `scopes` / `RbacScope`). Do not copy scope strings or path shapes by hand—import from the package so renames and new endpoints fail at compile time instead of at runtime.
