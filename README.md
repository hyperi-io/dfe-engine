# dfe-engine

The DFE control plane: an API and CLI that turn operator changes into git commits a deployment runs from.

[![Build Status](https://github.com/hyperi-io/dfe-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/hyperi-io/dfe-engine/actions)
[![License](https://img.shields.io/badge/license-BUSL--1.1-blue)](LICENSE)

## What it does

- Sources are defined once. The receiver's and loader's routing is compiled from them, so no app carries hand-written routing.
- Every change is a commit in the deployment's own git repo, and Argo CD applies it. The audit log is the git history.
- The dfe-schemas manifest is applied at boot: the core ClickHouse objects, the Kafka bootstrap topics, and each source's table when that source is deployed.
- A hunt runner spreads scheduled hunts across their interval and coordinates through ClickHouse alone.
- OIDC login, SCIM 2.0 provisioning for accounts and groups, and scoped RBAC.
- The `dfe` CLI, generated from the API's own OpenAPI spec.

Records never flow through the engine. They go receiver, then Kafka or gRPC, then loader, then ClickHouse, and the engine decides what those apps are configured to do. DFE lands data first and lets you shape it later, one field at a time, with no reindex. [docs/data-evolution.md](docs/data-evolution.md) explains the three stages and the `promote` workflow.

DFE is a product that other organisations deploy. Nothing in this repo is specific to any one deployment, and configuration is injected at deploy time.

## Quick start

```bash
git clone https://github.com/hyperi-io/dfe-engine.git
cd dfe-engine
uv sync   # pulls dfe-schemas, which carries the schema and DDL trees
```

Run the API server. A clone has no `.env`, so set `DFE_ENGINE_ENV=dev`, without which the engine refuses to start on the placeholder JWT secret, and `DFE_CONFIG_DIR=./config`, without which it tries to create `/app/config`. `/livez` answers on port 8000:

```bash
env DFE_ENGINE_ENV=dev DFE_CONFIG_DIR=./config uv run dfe-engine
```

With no ClickHouse reachable it retries for a minute, then serves with `/readyz` reporting `clickhouse: false`. The local ClickHouse settings, the observability port and the other repos are in [docs/local-dev.md](docs/local-dev.md).

Run the unit tests, which need no backing service:

```bash
uv run pytest tests/unit -q
```

The entry points are `dfe-engine` (the API server), `dfe` (the CLI), `dfe-hunt-runner` and `dfe-keda-shim` (`pyproject.toml`, `[project.scripts]`).

## Configuration

Every setting is an environment variable with the `DFE_ENGINE_` prefix, apart from `DFE_CONFIG_DIR`, which other suite apps read too. `src/dfe_engine/settings.py` is the reference. The two directory roots:

| Variable | Image default | Local-dev default | What it holds |
|----------|---------------|-------------------|---------------|
| `DFE_CONFIG_DIR` | `/app/config` | `./config` with `make dev`, else `/app/config` | The deployment's config: a mounted volume or ConfigMap on Kubernetes, a checkout of the deployment's config repo for local dev. The engine finds `sources/`, `source-builds/`, `source-plans/`, `source-deploys/`, `fieldmaps/`, `services/`, `hunts/`, `rules/` and `alert-destinations/` under it. |
| `DFE_ENGINE_SCHEMAS_DIR` | `/app/schemas` | unset | The schema tree root. Unset, the engine reads the trees from the installed `dfe-schemas` package. |

The image bakes the dfe-schemas trees in at `/app/schemas` and an empty `/app/config`, so neither directory variable needs setting in a container. The deployment supplies the config by mount and the secrets and connection settings by env, and a container with none of those refuses to start on the placeholder JWT secret. A per-registry variable such as `DFE_ENGINE_SOURCES_DIR` wins over the subdirectory derived from `DFE_CONFIG_DIR`. The names are exact: a bare `CONFIG_DIR` is not read. ClickHouse and Kafka connection settings follow the same prefix.

## Documentation

- [docs/architecture.md](docs/architecture.md): what the engine is, its invariants and why it is shaped this way.
- [docs/control-plane/](docs/control-plane/README.md): the API, auth, RBAC and governed gitops.
- [docs/data-plane/](docs/data-plane/README.md): sources, schemas, DDL, the query API and hunts.
- [docs/deployment/](docs/deployment/README.md): the chart, the image and the deployment contract.
- [docs/reference/](docs/reference/README.md): the API and CLI reference.

The suite's published docs are at https://dfe.docs.hyperi.io, and [dfe-infra](https://github.com/hyperi-io/dfe-infra) deploys the whole suite.

## Help and contributing

How to work in this repo, the commands that prove a change and what tends to bite are in [CONTRIBUTING.md](CONTRIBUTING.md). A question that is not a bug goes in a new issue.

Open bugs: [this repo](https://github.com/hyperi-io/dfe-engine/issues?q=is%3Aopen+is%3Aissue+type%3ABug+-label%3Abacklog+-label%3Anext-minor) | [whole DFE suite](https://github.com/search?type=issues&q=is%3Aopen+is%3Aissue+type%3ABug+-label%3Abacklog+-label%3Anext-minor+repo%3Ahyperi-io%2Fdfe-infra+repo%3Ahyperi-io%2Fdfe-docker+repo%3Ahyperi-io%2Fdfe-deploy+repo%3Ahyperi-io%2Fdfe-engine+repo%3Ahyperi-io%2Fdfe-loader+repo%3Ahyperi-io%2Fdfe-receiver+repo%3Ahyperi-io%2Fdfe-fetcher+repo%3Ahyperi-io%2Fdfe-archiver+repo%3Ahyperi-io%2Fdfe-transform-vrl+repo%3Ahyperi-io%2Fdfe-transform-vector+repo%3Ahyperi-io%2Fdfe-transform-elastic+repo%3Ahyperi-io%2Fdfe-ui+repo%3Ahyperi-io%2Fdfe-hyperdx+repo%3Ahyperi-io%2Fscalo-rs+repo%3Ahyperi-io%2Fscalo-py+repo%3Ahyperi-io%2Flogreducer+repo%3Ahyperi-io%2Fdfe-schemas+repo%3Ahyperi-io%2Fvector-vrl+repo%3Ahyperi-io%2Fculvert+repo%3Ahyperi-io%2Fdfe-clickhouse-rs+repo%3Ahyperi-io%2Ffactbook-rs)

## License

BUSL-1.1, see [LICENSE](LICENSE).
