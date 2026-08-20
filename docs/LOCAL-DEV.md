# Running dfe-engine locally

The fastest way to work on the engine is to run it as a plain local process -- a
`uv` virtualenv and one command, no containers and no Kubernetes. This is the
setup a contributor uses to iterate on an endpoint, and the one the auth/attributes
and rule-authoring work is developed against.

## The 30-second boot

```sh
git submodule update --init      # first time only -- populates schemas/
make dev                         # -> Swagger UI at http://localhost:8003/docs
```

`make dev` exports `DFE_CONFIG_DIR=./config` and `DFE_SCHEMAS_DIR=./schemas` and
runs `uv run dfe-engine run`. The console script `dfe-engine` maps to
`dfe_engine.api:run_dev_server`, which serves the API on `DFE_API_PORT` (default
8003).

Two things trip people up on a fresh clone:

- **`config/` is not a submodule** -- only `schemas` is. `make dev` points
  `DFE_CONFIG_DIR` at `./config`; the engine seeds its auth store, registries and
  `.secrets` into whatever that directory is, and tolerates it being empty or a
  throwaway. It does NOT need to pre-exist with content.
- **You need a `.env`** for `make dev` (`cp .env.example .env`). For pure
  auth/attributes work you do not want the example's ClickHouse pointed at a real
  cluster -- set `DFE_CLICKHOUSE_BOOTSTRAP_TABLES=false` and the engine boots with
  no ClickHouse at all.

## The no-dependencies auth spike

To exercise the auth, accounts, groups or attributes endpoints with nothing else
running -- no ClickHouse, no FerretDB, no deploy repo:

```sh
env DFE_ENV=dev \
    DFE_AUTH_ENABLED=false \
    DFE_AUTH_STORE_BACKEND=yaml \
    DFE_GITOPS_ENABLED=false \
    DFE_CLICKHOUSE_BOOTSTRAP_TABLES=false \
    DFE_API_PORT=8003 \
    DFE_CONFIG_DIR=/tmp/dfe-config \
    DFE_SCHEMAS_DIR=./schemas \
    uv run dfe-engine run
```

- **No token needed.** `DFE_AUTH_ENABLED=false` with `DFE_ENV=dev` hands every
  unauthenticated request the `admin` role, so you can call any endpoint straight
  from `curl`/Swagger. The engine still mints an ephemeral ES384 JWT authority at
  boot; you just do not need it on this path. (The offline `auth` subcommands live
  on the separate `dfe` CLI, not the `dfe-engine` daemon.)
- **`store_backend: auto` falls to yaml** when no document-store URI is configured
  (`DFE_AUTH_ACCOUNTS_STORE_URI` empty), so you get a file-backed account/group
  store under `DFE_CONFIG_DIR/auth` with no FerretDB. Force it with
  `DFE_AUTH_STORE_BACKEND=yaml` if you want to be explicit.
- **Attribute request bodies are wrapped.** `PUT
  /api/v1/auth/accounts/{username}/attributes` takes `{"attributes": {...}}`, not
  the bare object, and the `GET` returns the same shape. A bare object is a 422.

## Running against the interdependent repos

The engine is the OIDC issuer and control plane the rest of the local stack trusts.
When Kay runs dfe-engine + dfe-hyperdx + dfe-ui + a local dfe-deploy clone together
(all as git clones, not containers), the wiring is:

- **dfe-hyperdx (the fork)** trusts the engine's signing key over JWKS. Point the
  fork at `DFE_ENGINE_JWKS_URL=http://localhost:8003/api/v1/auth/jwks` (its
  `engineOrigin()` derives the engine base URL from that). The fork's `/dfe/*`
  routes forward the caller's `dfe_token` to the engine.
- **dfe-ui** talks to the engine API at `http://localhost:8003/api` and embeds the
  fork; the fork's create-rule button opens `${DFE_UI_BASE_URL}/rules/{id}`.
- **The deploy repo (dfe-deploy)** is the gitops target. Either disable gitops
  (`DFE_GITOPS_ENABLED=false`) or point it at your local clone with
  `DFE_GITOPS_REPO_URL`/`DFE_GITOPS_LOCAL_PATH`; with it off the live auth store IS
  the durable store, so there is nothing to commit.

> The single-process boot above is verified. The full four-repo local stack is the
> target for the per-repo local-dev docs (see each repo's `CONTRIBUTING`); bring it
> up one repo at a time, engine first, and confirm each speaks to the engine before
> adding the next.
