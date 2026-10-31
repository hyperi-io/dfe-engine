# Running dfe-engine locally

The fastest way to work on the engine is to run it as a plain local process -- a
`uv` virtualenv and one command, no containers and no Kubernetes. This is the
setup a contributor uses to iterate on an endpoint, and the one the auth/attributes
and rule-authoring work is developed against.

## The 30-second boot

```sh
make dev                         # -> Swagger UI at http://localhost:8003/docs
```

`make dev` exports `DFE_CONFIG_DIR=./config` and runs `uv run dfe-engine run`.
The console script `dfe-engine` maps to `dfe_engine.api:run_dev_server`, which
serves the API on `DFE_API_PORT`: 8000 in code, 8003 from `.env.example`, and
8003 is what `make dev` and the other repos' local docs assume.

Two things trip people up on a fresh clone:

- **The schemas come from the `dfe-schemas` package**, which `uv sync` installs
  -- there is nothing to check out. `DFE_SCHEMAS_DIR` is deliberately left unset
  by `make dev`; set it only to point the engine at your own tree. `config/` is
  a plain directory: `make dev` points `DFE_CONFIG_DIR` at `./config`, the engine
  seeds its auth store and registries into whatever that directory is, and
  tolerates it being empty or a throwaway. It does NOT need to pre-exist with
  content. The minted signing key and per-group ClickHouse passwords go to
  `.secrets/` in the CURRENT directory (`DFE_SECRETS_PATH`, default `./.secrets`),
  not under `DFE_CONFIG_DIR`, so a second engine started from the same directory
  reuses that key.
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

## A local ClickHouse

The client is clickhouse-connect over HTTP, so the port is the HTTP one and a
plain docker ClickHouse is not TLS:

```sh
env DFE_ENV=dev \
    DFE_CLICKHOUSE_HOST=127.0.0.1 \
    DFE_CLICKHOUSE_PORT=8123 \
    DFE_CLICKHOUSE_SECURE=false \
    DFE_CLICKHOUSE_PASSWORD=... \
    DFE_CONFIG_DIR=/tmp/dfe-config \
    uv run dfe-engine run
```

Get it wrong and the engine logs `SSL: WRONG_VERSION_NUMBER` (TLS against a
plain port) or connection refused (the native 9000), retries for a minute, then
serves degraded with `/readyz` answering 503 `{"clickhouse": false}` while
`/livez` stays alive. Tenant isolation uses ClickHouse custom settings under the
`SQL_` prefix; dfe-docker's `clickhouse/server-custom.xml` declares that prefix
and a stock image does not.

## Port collisions

`dfe-engine run` binds the observability listener (`/metrics`, `/livez`,
`/readyz`) on `0.0.0.0:9090` before the API. With the dfe-docker stack up that
port is taken and the process dies with `fatal: [Errno 98] Address already in
use` and no port named. The knob is scalo's, not `DFE_`-prefixed:

```sh
env METRICS_ADDR=127.0.0.1:9197 DFE_ENV=dev uv run dfe-engine run
```

## No collector running

The OTLP exporters aim at `localhost:4317` and log `Transient error ... retrying`
every few seconds while nothing listens. scalo reads a whitespace-only endpoint
as "no endpoint", which is its off switch:

```sh
env OTEL_EXPORTER_OTLP_ENDPOINT=" " DFE_ENV=dev uv run dfe-engine run
```

## Running against the interdependent repos

The engine is the OIDC issuer and control plane the rest of the local stack trusts.
When Kay runs dfe-engine + dfe-hyperdx + dfe-ui + a local dfe-deploy clone together
(all as git clones, not containers), the wiring is:

- **dfe-hyperdx (the fork)** trusts the engine's signing key over JWKS. Point the
  fork at `DFE_ENGINE_JWKS_URL=http://localhost:8003/.well-known/jwks.json` (the
  standard OIDC discovery path; its `engineOrigin()` derives the engine base URL
  from that). Set `DFE_ENGINE_ISSUER=https://dfe.local/api` to match the engine's
  advertised issuer (`/.well-known/openid-configuration`), enforced as the token
  `iss`. The fork's `/dfe/*` routes forward the caller's `dfe_token` to the engine.
  Under a dev `DFE_ENV` that cookie is set without `Secure`, so a plain-http stack
  keeps it; production sets `Secure`, like the session cookie.
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
