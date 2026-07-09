# Live full-stack e2e suite

`test_live_pipeline.py` - the 4 CRITICAL e2e tests, run against a REAL deployed
DFE (not the in-process TestClient suite). Marked `@pytest.mark.live` and EXCLUDED
from the default run (`-m 'not integration and not live'`); they SKIP unless the
`DFE_E2E_*` env vars point at a deployment. No mocks - every step hits a real
endpoint.

## The 4 tests

1. **HTTPS -> ClickHouse** (`test_https_ingest_lands_in_clickhouse`)
   POST a uniquely-tagged event to the receiver over HTTPS; assert it lands in
   the `dfe.default` ClickHouse table.
2. **-> HyperDX** (`test_ingested_data_visible_in_hyperdx`)
   Ingest, confirm it is in CH, then confirm HyperDX's search API returns it
   (proves the HyperDX -> CH datasource wiring).
3. **dfe-ui infra change via git** (`test_ui_deploys_infra_change_via_git`)
   Read a deployment config via the engine API, change a Helm var, publish, and
   assert the change reached the deploy repo (`values/<svc>-<inst>-values.yaml`).
4. **dfe-ui schema change via git** (`test_ui_deploys_schema_change_via_git`)
   Create a source/meta-schema via the engine API, publish, and assert the
   generated DDL (`ddl/<table>.sql`) reached the deploy repo with the new column.

## Assumed deployment

A `single` or `standard` k8s tier (see docs/ARCHITECTURE.md "Deployment tiers"):
dfe-engine, dfe-ui, the receiver, ClickHouse, HyperDX, and a reachable deploy
repo. Tests 1-2 exercise the data plane; 3-4 exercise the control plane (engine
git write -> deploy repo, which Argo then applies).

## Run

    export DFE_E2E_RECEIVER_URL=https://dfe.<domain>          # or the receiver LB
    export DFE_E2E_CH_HOST=<clickhouse-host>                  # + _CH_USER/_CH_PASSWORD/_CH_DB
    export DFE_E2E_HYPERDX_URL=https://hyperdx.<domain>       # + _HYPERDX_API_KEY
    export DFE_E2E_ENGINE_URL=https://dfe.<domain>            # + _ENGINE_TOKEN
    export DFE_E2E_DEPLOY_REPO_URL=https://.../deploy.git     # + _DEPLOY_REPO_TOKEN/_USER
    uv run pytest tests/e2e/test_live_pipeline.py -m live -o addopts=""

Each var that is missing skips only the tests that need it (the others still run).

## Status

Scaffolded + collection/skip-validated 2026-06-29. NOT yet run green against a
live deployment - that needs the dfe apps actually deployed (blocked on the GHCR
pull secret on the test cluster + the receiver `/ingest` HTTPS path + the engine
`/deployments/publish` endpoint name, which should be confirmed against the real
API once a full deployment is up).
