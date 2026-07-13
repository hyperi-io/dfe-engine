# DFE Engine

The config control plane of the Data Fusion Engine (DFE) product suite - a
Python library plus FastAPI server that turns operator intent into governed
git commits (YAML config, Helm overlay values, ClickHouse DDL) which Argo CD
reconciles into the cluster.

## A product suite, not an internal tool

DFE is a product suite that any organisation deploys to run its own data-fusion
engine. Our own hosted instance is just ONE deployment, not "the product". Every
core DFE repo (engine, infra, schemas, ui, hyperdx, and the Rust data-plane apps)
is public by design.

What that means for the code here:

- **Keep it generic.** No environment-specific names, hostnames, cluster IDs,
  credentials, or private-infra assumptions belong in this repo.
- **Code to the seam, not to a deployment.** Configuration is injected; the
  engine codes to an interface (secrets backend, object store, cluster topology),
  never to a specific provider or to our infrastructure.
- **Test WITH a deployment, never FOR one.** Integration tests run against a real
  backing service, but the code must never assume that service is ours.

Anything specific to a particular deployment - our fleet, a customer's cluster -
lives in that deployment's own private config, never in this repo.

## Quick start

```bash
git clone https://github.com/hyperi-io/dfe-engine.git
cd dfe-engine
git submodule update --init   # config -> dfe-devex, schemas -> dfe-schemas
uv sync
```

Run the API server (health at `/health/live` on port 8000):

```bash
uv run dfe-engine
```

Run the unit tests (no backing services needed):

```bash
uv run pytest tests/unit -q
```

Entry points (`pyproject.toml [project.scripts]`): `dfe-engine` (the API
server daemon), `dfe` (the remote client CLI, generated from the OpenAPI
spec), plus the runtime helpers `dfe-hunt-runner` and `dfe-keda-shim`.

## What it does

The engine is BOTH a pip-installable library and the API server. Every
operational change - sources, schemas, hunts, deployment dials, access
control - is a versioned YAML change in a git repo, made through one
governed path, then reconciled to the cluster by Argo CD. The engine never
deploys backing services and never touches the cluster directly.

Full system map, invariants, and the docs tree:
[docs/architecture.md](docs/architecture.md).

| Area | Code | Docs |
|------|------|------|
| API + auth + RBAC | `src/dfe_engine/api/`, `auth/` | [control-plane/](docs/control-plane/index.md) |
| Governed gitops CRUD | `gitcrud/`, `gitops/`, `governance/`, `helm/` | [control-plane/governed-ops-design.md](docs/control-plane/governed-ops-design.md) |
| Sources, schemas, DDL | `source/`, `schema/`, `fieldmap/`, `cel/`, `sigma/` | [data-plane/](docs/data-plane/index.md) |
| Query API | `query/` | [data-plane/query-api.md](docs/data-plane/query-api.md) |
| Hunts | `hunts/`, `hunt_runner/`, `keda_shim/` | [data-plane/hunt-runner-scaling.md](docs/data-plane/hunt-runner-scaling.md) |
| Deployment seam | `deployment/`, `deployment_contract.py`, `chart/` | [deployment/](docs/deployment/index.md) |

## Configuration

All configuration is environment variables with the `DFE_` prefix
(`src/dfe_engine/settings.py` is the reference). The two directory roots:

| Variable | Image default | Local-dev default | Description |
|----------|---------------|-------------------|-------------|
| `DFE_CONFIG_DIR` | `/app/config` | `./config` | Config submodule root. Auto-resolves registry subdirs (`sources/`, `fieldmaps/`, `services/`, `deployment/`, `hunts/`, `hunt-rules/`, `rules/`, `alert-destinations/`, `queries/`). |
| `DFE_SCHEMAS_DIR` | `/app/schemas` | `./schemas` | Schema submodule root (`dfe-schemas`). |

- The Docker image bakes the `config/` and `schemas/` submodule trees in at
  `/app/config` and `/app/schemas`, and the Dockerfile sets `DFE_CONFIG_DIR`
  and `DFE_SCHEMAS_DIR` to those paths. A container therefore runs with no env
  set.
- Setting either variable at runtime (`docker run -e ...`, a K8s pod `env:`,
  or a local `.env`) overrides the baked-in default - for example, to point at
  a mounted volume or ConfigMap.
- For `DFE_CONFIG_DIR`, a more specific per-registry override such as
  `DFE_SOURCES_DIR` takes precedence over the subdirectory derived from
  `DFE_CONFIG_DIR`.
- The variable names are exact and `DFE_`-prefixed. A bare `CONFIG_DIR` or
  `SCHEMAS_DIR` (no prefix) is not read.

Backing-service connection settings (ClickHouse, Kafka) follow the same
`DFE_` pattern - see `settings.py` for the full set and defaults.

## Code standards

- Logging: `from scalo.logger import logger` - never stdlib `logging`.
- HTTP: `scalo.http` `HttpClient`/`AsyncHttpClient` - never raw httpx.
- YAML: `dfe_engine.yaml_utils` (ruamel, YAML 1.2).
- Settings: `from dfe_engine.settings import get_settings`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev workflow, commit format,
and DCO.

## Related repos

- [dfe-infra](https://github.com/hyperi-io/dfe-infra) - charts, ApplicationSets, bootstrap (the deployment vehicle)
- [dfe-ui](https://github.com/hyperi-io/dfe-ui) - web UI (consumes this API)
- [dfe-schemas](https://github.com/hyperi-io/dfe-schemas) - schema + DDL SSoT (the `schemas/` submodule)
- [dfe-hyperdx](https://github.com/hyperi-io/dfe-hyperdx) - extended HyperDX fork (explore UI + telemetry sink)
- [scalo-py](https://github.com/hyperi-io/scalo-py) - shared Python library (`scalo` on PyPI)

## License

Licensed under BUSL-1.1 - see [LICENSE](LICENSE).
