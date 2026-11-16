# Type safety sync (dfe-engine -> dfe-ui)

dfe-engine is the source of truth for the HTTP API contract and the RBAC scope catalogue, and `openapi.json` holds both. A workflow syncs it into dfe-ui, so the frontend uses strict TypeScript types and scope constants with no hand-kept duplicates.

- API types: routes, request bodies and responses defined in FastAPI and Pydantic become TypeScript types in dfe-ui.
- RBAC scopes: the permission strings in `src/dfe_engine/auth/rbac_scopes/scope_constants.py` become the `RbacScope` string enum in the spec's `components.schemas`, with their constant names in `x-enumNames` (`src/dfe_engine/api/app.py`), and so a generated constant in dfe-ui.
- Automation: a push to `main` or `master` that changes a spec opens a PR in dfe-ui, so frontend code is checked at compile time against the latest engine contract.

`scripts/generate_openapi.py` loads the live FastAPI app and writes the OpenAPI 3 schema to `openapi/openapi.json` and `openapi/openapi.e2e.json`, both committed. Regenerate after any model or route change:

```bash
uv run python scripts/generate_openapi.py
```

The Prism mock, the contract test (`tests/unit/test_openapi_spec_committed.py`) and dfe-ui's `openapi-typescript` generation read the committed files. A change to the Python API alone does not start the sync: commit the regenerated specs with the code change. [ui-api-guide.md](ui-api-guide.md) covers the local mock.

## The sync workflow

[`.github/workflows/sync-dfe-engine-types.yml`](../../.github/workflows/sync-dfe-engine-types.yml), `Sync dfe-engine-types`, runs on a push to `main` or `master` that changes `openapi/openapi.json` or `openapi/openapi.e2e.json`, and on `workflow_dispatch`. It:

1. Checks out dfe-engine.
2. Mints a token from the GitHub App and clones dfe-ui (`vars.DFE_UI_REPO`, default `hyperi-io/dfe-ui`).
3. Copies the two specs to `packages/dfe-engine-types/specs/` in dfe-ui.
4. Runs `yarn install --immutable` and `yarn generate` in `packages/dfe-engine-types`, which writes `types/index.d.ts`, `types/e2e.d.ts` (exported as `@repo/dfe-engine-types/e2e`), the scope constants and the limits.
5. Opens a PR in dfe-ui and closes older open sync PRs, so one sync PR is open at a time.

It needs `vars.GH_APP_CLIENT_ID` and `secrets.GH_APP_PRIVATE_KEY`: the GitHub App's installation token, scoped to dfe-ui with contents and pull-requests write, pushes the branch and opens and closes the PRs.

In dfe-engine, change the API or the scopes in Python, regenerate the specs, commit them with the code, and merge the dfe-ui PR the workflow opens. In dfe-ui, import from `packages/dfe-engine-types` and never copy a scope string or path by hand, so a rename or a new endpoint fails at compile time instead of at runtime.
