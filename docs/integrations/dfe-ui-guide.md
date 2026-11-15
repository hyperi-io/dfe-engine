<!-- Project: dfe-engine (cross-references dfe-ui, dfe-infra, hyperi-hyperdx) -->
# dfe-ui: how it works, how it deploys, how it talks to the engine

Written for someone who is NOT a front-end developer. It explains the moving
parts in plain terms, how it deploys, and what it does not do yet.

## 1. The one-paragraph mental model

dfe-ui is a **web app that is just a window onto the dfe-engine API**. It holds no
data of its own - it logs you in, then reads and writes config (sources, schemas,
rules, hunts, deployment dials) by calling the engine over HTTP. dfe-hyperdx (the data
explorer) is a **separate web app** that dfe-ui embeds in an iframe. So the
"product" the user sees is really three cooperating pieces: dfe-ui (control), the
engine (the brain + API), and dfe-hyperdx (data exploration).

## 2. What it is built from (the stack)

- **Next.js + React** (versions in `apps/dfe-core-ui/package.json`) - Next.js is a web framework that runs partly on a
  Node.js server and partly in the browser. Think "a Node server that renders
  pages and also ships interactive JavaScript to the browser".
- **A monorepo** (Yarn + Turbo): one repository holding several packages -
  `apps/dfe-core-ui` (the actual app) plus shared `packages/*`, among them
  `dfe-engine-types` (see below).
- It builds in **"standalone" mode**: `next build` produces a self-contained
  `server.js` that we run in the container with `node apps/dfe-core-ui/server.js`.

## 3. How it talks to the engine (and stays type-safe)

- The engine publishes an **OpenAPI spec** (`dfe-engine/openapi/openapi.json`) --
  a machine-readable description of every endpoint, its inputs and outputs.
- dfe-ui runs a tool (`openapi-typescript`) that turns that spec into TypeScript
  **types** (`packages/dfe-engine-types`). So the UI code knows, at compile time,
  the exact fields of every request and response. If the engine changes an endpoint
  and the spec updates, the UI fails to compile rather than breaking silently.
- A small hand-written client (`createApiClient`) uses those types for `get/post/
  put/delete`. List screens use TanStack Query's `useInfiniteQuery` against the
  engine's `Page` (`items` + `next_page_token`).

**Login / auth:** the UI uses NextAuth with a username/password form that POSTs to
the engine's `/api/v1/auth/login`, gets a JWT (bearer token), stores it in a secure
cookie, and attaches `Authorization: Bearer <token>` to every API call. On a 401 it
signs you out. An OIDC sign-in goes through the engine's own relying party, which
mints the same engine JWT ([auth-envoy-topology.md](../auth-envoy-topology.md)).

## 4. How dfe-hyperdx is embedded

dfe-hyperdx is a **sibling app**, embedded rather than linked out to. The integration
is deliberately light:

1. Embedded views - when `HYPERDX_URL` is set on the dfe-ui deployment, the
   sidebar gains `/observe/*` entries (Search, Search list, Hunt Results) that
   iframe the chromeless dfe-hyperdx fork inside dfe-ui. The `(auth)` layout reads
   the variable server-side at request time, so it is a deployment setting, not a
   build-time one.
2. "Create rule from a Search" - dfe-hyperdx can send the browser a
   cross-window message (`postMessage`). Dfe-ui catches it, stores the search, and
   opens its rule-create screen pre-filled. The UI checks the message came from the
   configured dfe-hyperdx origin.

So the integration is **one environment variable** (`HYPERDX_URL`, or `HYPERDX_PORT`)
plus a postMessage handshake. The engine does NOT tell the UI where dfe-hyperdx is - the
deployment does. `NEXT_PUBLIC_HYPERDX_URL` is read by nothing and setting it does
nothing.

## 5. How it deploys

Every dfe-ui release publishes the image `ghcr.io/hyperi-io/dfe-ui` and the Helm chart
`oci://ghcr.io/hyperi-io/charts/dfe-ui` at the same version.
hyperi-ci renders the chart from dfe-ui's committed deployment contract,
`deploy/contract.json`.

- Health: `src/core/health.ts` serves scalo's probe contract on the app's one
  port, 3000: `/livez` answers 200 while the process runs, and `/readyz` answers 200
  until the pod is told to drain. Neither checks the engine, so an engine outage does
  not pull the UI from its Service. The startup probe reuses `/livez`. Both paths
  bypass the auth middleware (`src/proxy.ts`), so a probe never reads a login redirect
  as success.
- NEXTAUTH_SECRET: The contract declares it as a required secret (group
  `nextauth`, key `secret`), so the chart injects it into the pod's environment.
- Engine address: The image is built with `NEXT_PUBLIC_API_URL` empty, so the
  browser calls the engine on its own origin, and the server side reads
  `INTERNAL_API_URL` at request time. One image serves every environment.
- dfe-hyperdx address: `HYPERDX_URL`, or `HYPERDX_PORT` on the UI's own host, is read
  at request time (section 4).

The `NEXT_PUBLIC_*` values left are the session refresh timings and a display suffix.
Next.js still inlines those at build, so changing one needs a rebuild.

## 6. Why the addresses are read at runtime

A Next.js quirk: public config (`NEXT_PUBLIC_*`) is **replaced with literal text
inside the compiled JavaScript at build time**. So you cannot change the API URL or
dfe-hyperdx URL of an already-built image - they are no longer variables, just baked-in
strings. Building a different image per environment is the anti-pattern everyone
warns against, which is why dfe-ui reads its engine and dfe-hyperdx addresses at runtime
(section 5).

## 7. What it does not do yet

- Runtime config from the engine: The engine serves `GET /api/v1/config/client`
  (the API base, the dfe-hyperdx URL and whether it is enabled, feature flags and the
  auth mode). dfe-ui shows it on the System Management page but still takes its
  dfe-hyperdx address from its own environment (section 4).
- Live updates: Lists are polled. The engine has no push channel: a long-running
  operation is polled at `GET /api/v1/operations/{operation}`.
- Generated hooks: List screens hand-write a `useInfiniteQuery` wrapper per
  endpoint over the generated types.

## 8. References

- Next.js runtime env and one image for every environment: <https://nextjs.org/docs/app/getting-started/deploying>
- Engine API contract for the UI: [../control-plane/ui-api-guide.md](../control-plane/ui-api-guide.md)
- Governed Ops model: [../control-plane/governed-ops-design.md](../control-plane/governed-ops-design.md) and [../architecture.md](../architecture.md)
