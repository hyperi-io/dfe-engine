# Live full-stack e2e suite

Tests that run against a REAL deployed DFE rather than the in-process TestClient
suite. Marked `@pytest.mark.live` and EXCLUDED from the default run
(`-m 'not integration and not live'`); they SKIP unless the `DFE_E2E_*` env vars
point at a deployment. No mocks -- every step hits a real endpoint.

Two files. `test_live_pipeline.py` proves the deployment works at all;
`test_filebeat_pipeline.py` proves the transform layer works on real data.

## test_live_pipeline.py -- the 4 critical tests

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

## test_filebeat_pipeline.py -- the transform layer on real data

The whole source path with a transform in it: receiver -> `filebeat_land` ->
a transform -> `filebeat_load` -> loader -> ClickHouse. Real cisco corpus data,
read from an archive in memory and never unpacked (it is Elastic-licensed).

Four tests: an event with the routing discriminator reaches its own source
table, one without it stays on the default path, the transform populates ECS
columns, and an unschema'd source still lands under the common header.

It is NOT parameterised over dfe-transform-vrl and dfe-transform-vector. They
run the same VRL, read the same land topic and emit to the same load topic, and
a landed row names neither, so two cases would be one experiment run twice.
`DFE_E2E_TRANSFORM` names the deployed app in the failure message only.

## Assumed deployment

A `single` or `standard` k8s tier (see docs/architecture.md "Deployment tiers"):
dfe-engine, dfe-ui, the receiver, ClickHouse, HyperDX, and a reachable deploy
repo. The filebeat file additionally needs a transform app deployed for the
`filebeat` source, and its table built from `meta/beats/filebeat.yaml`.

## Run

    export DFE_E2E_RECEIVER_URL=https://dfe.<domain>          # or the receiver LB
    export DFE_E2E_CH_HOST=<clickhouse-host>                  # + _CH_USER/_CH_PASSWORD/_CH_DB
    export DFE_E2E_HYPERDX_URL=https://hyperdx.<domain>       # + _HYPERDX_API_KEY
    export DFE_E2E_ENGINE_URL=https://dfe.<domain>            # + _ENGINE_TOKEN
    export DFE_E2E_DEPLOY_REPO_URL=https://.../deploy.git     # + _DEPLOY_REPO_TOKEN/_USER
    export DFE_E2E_TRANSFORM=dfe-transform-vrl                # which transform is deployed
    export DFE_E2E_VERIFY=1                                   # enforce TLS (default off)
    uv run pytest tests/e2e -m live -o addopts=""

Each var that is missing skips only the tests that need it. `DFE_E2E_VERIFY`
defaults OFF because a deployment usually fronts these with its own CA and the
tests assert the data path, not the cert chain.

## Status

`test_filebeat_pipeline.py` has been run against a live deployment. The routing
test passed. The rest failed, on two real product defects rather than on the
harness: the loader rejects IPv4 literals for an IPv6 column so every event
carrying `source.ip` is dropped (dfe-loader#127), and a batched receiver POST
was forwarded whole so the loader was handed an array where one event belongs
(dfe-loader#128, fixed in dfe-receiver).

Worth knowing before reading a pass as proof: the ECS assertion rests on
`log_file_path`, which the bundled VRL sets only in its cisco_umbrella branch.
Of the events a run posts, roughly a third can satisfy it, so a green result
says the umbrella path transformed -- not that all three corpus modules did.

`test_live_pipeline.py` has NOT been run green end to end.
