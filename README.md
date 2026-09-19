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
uv sync   # pulls dfe-schemas, which carries the schema + DDL trees
```

Run the API server. A clone has no `.env`, so name the dev posture -- without it
the engine refuses to start on the placeholder JWT secret. `/livez` answers on
port 8000:

```bash
env DFE_ENV=dev uv run dfe-engine
```

With no ClickHouse reachable it retries for a minute, then serves with `/readyz`
reporting `clickhouse: false`. The local ClickHouse settings, the observability
port, and the other repos are in [docs/LOCAL-DEV.md](docs/LOCAL-DEV.md).

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

**Working here** -- entry points, the commands that prove a change, and what
tends to bite: [CONTEXT.md](CONTEXT.md).

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
| `DFE_CONFIG_DIR` | `/app/config` | `./config` | Config directory - the deployment supplies it (a mounted volume/ConfigMap in k8s; a checkout of the deployment's config repo, e.g. dfe-devex, for local dev). Auto-resolves registry subdirs (`sources/`, `fieldmaps/`, `services/`, `deployment/`, `hunts/`, `hunt-rules/`, `rules/`, `alert-destinations/`, `queries/`). |
| `DFE_SCHEMAS_DIR` | `/app/schemas` | unset | Schema tree root. Unset, the engine reads the trees out of the installed `dfe-schemas` package. |

- The Docker image bakes the `dfe-schemas` package's trees in as a seed at
  `/app/schemas`; `/app/config` is an empty baked default. The Dockerfile sets
  `DFE_CONFIG_DIR` and `DFE_SCHEMAS_DIR` to those paths, so a container runs
  with no env set - config is supplied at deploy (a mounted volume/ConfigMap).
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

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev workflow, commit format,
and DCO.

## License

Licensed under BUSL-1.1 - see [LICENSE](LICENSE).

## Context

### What this is

The control plane for a DFE deployment: a Python library plus an API server that
owns configuration, schema and app lifecycle. It is NOT the data path -- records
never flow through here. They go receiver -> Kafka or gRPC -> loader ->
ClickHouse, and this engine decides what those components are configured to do.

System design and why it is shaped this way:
[docs/architecture.md](docs/architecture.md).

### Where things live

| path | what |
|---|---|
| `src/dfe_engine/api/` | FastAPI routers, four auth paths, RBAC |
| `src/dfe_engine/appmgmt/` | one generic per-app surface; the catalogue is DATA |
| `src/dfe_engine/schema/` | ClickHouse DDL planning and apply |
| `src/dfe_engine/source/` | Source definitions; receiver and loader routing is COMPILED from these |
| `src/dfe_engine/gitops/` | every mutation is a git commit; the deploy repo is the authority |
| `docs/superpowers/` | specs and plans, including designs that are NOT built yet |

### Commands that prove a change

```
uv sync                       # deps, from the committed uv.lock
uv run pytest                 # the suite
uv run ruff check src tests   # lint
hyperi-ci check --quick       # what CI will say, faster
```

`ruff format` is available but the repo has not adopted it; there is no `black`.

**`uv run pytest` does not run everything.** The default `addopts` deselects the
`integration`, `live` and `upstream` markers, so a green local run has not
touched a backing service. Pass `-m integration` to include them.

Green locally is not green in CI. **A CI run is not atomic against hyperi-ci**:
reusable workflows resolve `@main` per JOB, so a push-event run reports success
with every real job SKIPPED. Read the `pull_request` run, per job, and check the
durations -- a test job that "passed" in 0s did not run.

### What tends to bite

| Don't | Do | Why |
|---|---|---|
| Hardcode an app name in engine code | Add it to `dfe-infra/apps.yaml` | What an app IS is DATA. Adding one is a manifest edit plus a chart, never an engine release. |
| Treat this as an internal tool | Code to the injected seam | It is a PRODUCT other organisations deploy. Env-specifics -- cluster refs, cred paths, our fleet -- belong in private config repos, never here. |
| Hand-edit compiled routing | Change the Source | Receiver and loader routing is derived; the API reports a hand edit as DRIFT and re-syncs over it. |
| Mock a backing service | testcontainers, a real one | No mocks as proof. `pass` and `TODO` are not functionality. |
| `from typing import List, Dict, Optional` | `list[str]`, `str \| None` | Built-in generics; `requires-python = ">=3.12"`. |
| `datetime.utcnow()` | `datetime.now(UTC)` | Deprecated since 3.12. |
| stdlib `logging` | `from scalo.logger import logger` | House rule, so every service emits one structured JSON shape. |
| Raw `httpx` with hand-rolled retry | `scalo.http.HttpClient` | House rule. Importing httpx for its exception and response TYPES is correct and expected. |
| `yaml.safe_load` | `dfe_engine.yaml_utils` | ruamel, YAML 1.2. YAML 1.1 reads `no` as boolean false. |
| `except Exception:` then a silent fallback | Catch the specific error, log with context, re-raise | 12 sites already return `False`/`None`/`[]` with no trace, out of 251 `except Exception:` sites in `src/`. An operator cannot tell failure from empty. |
| Assume a config key does something | Grep for its consumer | Config that parses and is never read is a recurring defect here. |
| Trust `gh issue list` | `gh issue view <n>` | It caps at 30 without `--limit`; absence is not evidence of closure. |

Two traps that are not style:

**`rg` and `fd` respect `.gitignore`**, so `.env` and `CLAUDE.md` are invisible
to a default search. Pass `--no-ignore` or you will conclude they do not exist.

**Every container this repo starts comes down the same session.** Stray
containers caused a host OOM. Only ever stop what you started.

### Where this sits

Membership is declared in `dfe-infra/suite.yaml` -- read it rather than trusting
a list, and generate any dependency claim from
`dfe-stack suite --consumer dfe-engine`.

The repo-by-repo map is in
[docs/architecture.md](docs/architecture.md#where-this-repo-sits-in-the-suite).
What belongs here is the consequence for someone about to change something:

**dfe-engine is the ONLY controller of ClickHouse objects and Kafka topics.** So
a change to schema, source compilation or topic naming is a SUITE-WIDE move, not
a local one -- every Rust service reads configuration this engine writes, and
infra and docker wait on it. Check `dfe-stack suite --producer dfe-engine`
before assuming a change stops at this repo.

Closest neighbours: [dfe-infra](https://github.com/hyperi-io/dfe-infra) (charts
and bootstrap, the deployment vehicle),
[dfe-ui](https://github.com/hyperi-io/dfe-ui) (consumes this API),
[dfe-schemas](https://github.com/hyperi-io/dfe-schemas) (schema and DDL SSoT),
[dfe-hyperdx](https://github.com/hyperi-io/dfe-hyperdx) (explore UI and
telemetry sink), [scalo-py](https://github.com/hyperi-io/scalo-py) (the shared
Python library).

**Declared dependencies** are in `pyproject.toml`; read them there rather than
trusting a list. Two that surprise people: `dfe-schemas` is a PACKAGE, not a
submodule, and `logreducer` is NOT a dependency -- it is a commented intention
pending a SHA pin.
