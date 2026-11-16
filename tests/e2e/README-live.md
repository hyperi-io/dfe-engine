# Live full-stack e2e suite

Tests that run against a REAL deployed DFE rather than the in-process TestClient
suite. Marked `@pytest.mark.live` and EXCLUDED from the default run
(`-m 'not integration and not live'`), and they SKIP unless the
`DFE_E2E_*` env vars point at a deployment. No mocks -- every step calls a deployed
endpoint.

`test_live_pipeline.py` checks the deployment works at all.
`test_filebeat_pipeline.py` checks the transform layer on corpus data.
`test_data_evolution.py` walks the data-evolution path step by step, below.
`test_governed_ops_live.py` drives
`POST /api/v1/governance/ch-rbac/reconcile` and asserts the seeded quota-tier and
service roles materialise as ClickHouse objects, covering both RBAC axes. It needs `DFE_E2E_ENGINE_URL`/`_TOKEN`, plus the `_CH_*` vars
for its CH assertions. `flows/` is the flow suite, below.

`engine_api.py` is the authenticated engine-API client both the flow suite and
the evolution suite drive the control plane with.

## flows/ -- every source case, landing or refused

`flows/fixtures/<shape>/` is the single fixture set: `source.yaml` (the create
bodies verbatim), `payload.ndjson` (`{marker}` is substituted per run) and
`expect.yaml` (the table, the topic on the bus, the transform's marker field, the
receiver destination on direct). Adding a case is a directory, not a test.
`flows/test_flows.py` is parametrised over cases x transports, and `flows/shapes.py`
parses and validates the fixtures and holds the transport rule.

`DFE_E2E_TRANSPORT` says which data path to prove: `kafka`, `grpc`, or `both`.
A deployment has ONE transport, so `both` proves a landing on the transport
it has and a REFUSAL on the other: the engine refuses a source naming a
transport nothing here serves, and that refusal is the assertion. Which is which
comes from `GET /api/v1/system/deployment`, so `both` is honest on `scale` and
`single` (bus) and on `slim` and `mesh` (direct) alike.

Every skip has to be one a fixture declared: `flows/conftest.py` fails the whole
run on any other, because a suite that passes with half its cases skipped
has proved nothing. A declared skip has the `EXPECTED-SKIP:` marker and says
what is missing -- whichever half of a case's pair the other half
proved, the catalogue case where no catalogue is mounted, and every case
that needs a fresh source's routing at the receiver where the deployment reports
`applies_routing: false` (a Compose stack mounts each app's config file
read-only, so the overlay stops at the deploy repo).

Both lanes run this same suite from a checkout of this repo:
`dfe-ops acceptance --suite flows` for Kubernetes, `make test-flows` in
dfe-docker for a compose stack. Neither holds a copy of the fixtures.

## test_live_pipeline.py -- the 3 critical tests

1. `test_https_ingest_lands_in_clickhouse` POSTs a uniquely-tagged event to the
   receiver over HTTPS and asserts it lands in the `dfe.main` ClickHouse table.
2. `test_ingested_data_visible_in_hyperdx` ingests, confirms the row is in CH,
   then confirms HyperDX's search API returns it, which proves the HyperDX to CH
   datasource wiring.
3. `test_ui_deploys_schema_change_via_git` creates a source via the engine API
   and asserts the generated DDL (`ddl/<table>.sql`) reached the deploy repo with
   the new column. The engine writes no `ddl/` directory: it applies the DDL to
   ClickHouse itself, so this test fails as written.

## test_data_evolution.py -- the acceptance path, step by step

One test per step of `docs/data-evolution-acceptance.md`, named for its number,
in the order the path runs: a record lands with nothing declared, a nested
`_json` path answers, a shipped schema refuses a write, a field promotes, a
source gets its routing and typed table, an index moves on the live table,
and a transform parses a syslog line into columns.

A step that cannot pass yet is `xfail(strict=True)` naming its open issue, so
the run goes red when the cause is fixed rather than passing quietly. 2.7 is
dfe-engine#1173. Nothing here is softened to make a run pass.

The steps share one source and one table, so run this file in ONE worker --
pass `-p no:xdist`, because workers would split the sequence across
processes that fight over it. Two waits are the product's, not
the harness's: a fresh source's first record waits on dfe-loader's 60 s topic
refresh, and a promoted column cannot be judged inside the loader's 300 s
directive cache, so 1.3 sends a fresh record each poll until the cache turns
over.

2.7 additionally needs the per-source transform instance RUNNING and the
receiver restarted onto the fresh source's routing. A deploy does neither, and the
API reports `restart_required: []` while the receiver still needs one.

Each 1.3 run leaves a forked schema behind. `meta/syslog` is core, so promote
forks it to `<source>_syslog` before adding the column. Teardown removes the
source and drops its table, and deliberately leaves the fork, because deleting a
schema some other version may reference is not a test's call. On a long-lived
stack they accumulate one per run and are cleared by hand.

## test_filebeat_pipeline.py -- the transform layer on corpus data

The whole source path with a transform in it: receiver -> `filebeat_land` ->
a transform -> `filebeat_load` -> loader -> ClickHouse. Real cisco corpus data,
read from an archive in memory and never unpacked (it is Elastic-licensed).

Four tests: an event with the routing discriminator reaches its source
table, one without it stays on the default path, the transform populates ECS
columns, and an unschema'd source still lands under the common header.

It is NOT parameterised over dfe-transform-vrl and dfe-transform-vector. They
run the same VRL, read the same land topic and emit to the same load topic, and
a landed row names neither, so two cases would be one experiment run twice.
`DFE_E2E_TRANSFORM` names the deployed app in the failure message and nowhere else.

`test_external_pipelines.py` puts the customer's pipeline in both places it
plugs in: a running Vector as a source's origin, and a running Fluent Bit as its
transform, each configured from the source's `connection` block. A compliant
record, a bare one and one claiming another tenant each land once, in the
source's org under its name. The containers run on the docker daemon the
shell points at, so they need a broker they can reach: `DFE_E2E_KAFKA_BOOTSTRAP`,
plus `DFE_E2E_DOCKER_NETWORK` for a dfe-docker stack's compose network. Config is
copied in rather than mounted, so a remote daemon works.

## Running it

The suite assumes a `single` or `scale` k8s tier (see [docs/deployment/README.md](../../docs/deployment/README.md)):
dfe-engine, dfe-ui, the receiver, ClickHouse, HyperDX, and a reachable deploy
repo. The filebeat file additionally needs a transform app deployed for the
`filebeat` source, and its table built from `meta/beats/filebeat.yaml`.

    export DFE_E2E_RECEIVER_URL=https://dfe.<domain>          # or the receiver LB
    export DFE_E2E_CH_HOST=<clickhouse-host>                  # + _CH_USER/_CH_PASSWORD/_CH_DB
    export DFE_E2E_HYPERDX_URL=https://hyperdx.<domain>       # + _HYPERDX_API_KEY
    export DFE_E2E_ENGINE_URL=https://dfe.<domain>            # + _ENGINE_TOKEN
    export DFE_E2E_DEPLOY_REPO_URL=https://.../deploy.git     # + _DEPLOY_REPO_TOKEN/_USER
    export DFE_E2E_TRANSFORM=dfe-transform-vrl                # which transform is deployed
    uv run pytest tests/e2e -m live -o addopts=""

Each var that is missing skips the tests that need it and no others.
`DFE_E2E_VERIFY=1` enforces TLS verification. It is left OFF above because a
deployment usually fronts these with a private CA and the tests assert the data
path, not the cert chain. Set it when the endpoints have a publicly-rooted certificate.

## Status

`test_filebeat_pipeline.py` has been run against a live deployment. The routing
test passed. The rest failed, on two product defects rather than on the
harness: the loader rejects IPv4 literals for an IPv6 column so every event
carrying `source.ip` is dropped (dfe-loader#127), and a batched receiver POST
was forwarded whole so the loader was handed an array where one event belongs
(dfe-loader#128, fixed in dfe-receiver).

Worth knowing before reading a pass as proof: the ECS assertion rests on
`log_file_path`, which the bundled VRL sets in its cisco_umbrella branch and nowhere else.
Of the events a run posts, roughly a third can satisfy it, so a passing result
says the umbrella path transformed -- not that all three corpus modules did.

`test_live_pipeline.py` has NOT passed as a whole run.

`test_data_evolution.py` has been run against the 2.2.2 compose stack. 1.3, 3.2
and 3.3 pass and 2.7 is the one xfail. 1.1 expects a 409 where the engine lets a
core schema's `current` move and answers 422 for an unknown version, and 2.6 saw
its active-part count fall from 5 to 1 across the add and drop. A failure in this
file is as likely to be the harness as the product.
