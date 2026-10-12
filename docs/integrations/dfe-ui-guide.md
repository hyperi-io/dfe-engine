<!-- Project: dfe-engine (cross-references dfe-ui, dfe-infra, hyperi-hyperdx) -->
# dfe-ui: how it works, how it deploys, how it talks to the engine

Written for someone who is NOT a front-end developer. It explains the moving
parts in plain terms, how it deploys, and where we are taking it - especially
under the Governed Ops model.

## 1. The one-paragraph mental model

dfe-ui is a **web app that is just a window onto the dfe-engine API**. It holds no
data of its own - it logs you in, then reads and writes config (sources, schemas,
rules, hunts, deployment dials) by calling the engine over HTTP. HyperDX (the data
explorer) is a **separate web app sitting beside it**, not built into it. So the
"product" the user sees is really three cooperating pieces: dfe-ui (control), the
engine (the brain + API), and HyperDX (data exploration).

## 2. What it is built from (the stack)

- **Next.js 16 + React 19** - Next.js is a web framework that runs partly on a
  Node.js server and partly in the browser. Think "a Node server that renders
  pages and also ships interactive JavaScript to the browser".
- **A monorepo** (Yarn 4 + Turbo): one repository holding several packages -
  `apps/dfe-core-ui` (the actual app) plus shared `packages/*` (an icon set, a
  logger, and `dfe-engine-types` - see below).
- It builds in **"standalone" mode**: `next build` produces a self-contained
  `server.js` that we run in the container with `node apps/dfe-core-ui/server.js`.

## 3. How it talks to the engine (and stays type-safe)

This part is genuinely well done:

- The engine publishes an **OpenAPI spec** (`dfe-engine/openapi-spec/openapi.json`) --
  a machine-readable description of every endpoint, its inputs and outputs.
- dfe-ui runs a tool (`openapi-typescript`) that turns that spec into TypeScript
  **types** (`packages/dfe-engine-types`). So the UI code knows, at compile time,
  the exact shape of every request and response. If the engine changes an endpoint
  and the spec updates, the UI fails to compile rather than breaking silently.
- A small hand-written client (`createApiClient`) uses those types for `get/post/
  put/delete`. List screens use TanStack Query's `useInfiniteQuery` against the
  engine's `PaginatedResponse` (`items` + `next_page`).

**Login / auth:** the UI uses NextAuth with a username/password form that POSTs to
the engine's `/api/v1/auth/login`, gets a JWT (bearer token), stores it in a secure
cookie, and attaches `Authorization: Bearer <token>` to every API call. On a 401 it
signs you out. (In production the intent is OIDC at the edge via Envoy + the
`X-Oidc-*` headers - the same seam the engine already supports.)

## 4. How HyperDX "auto-integrates" (today)

HyperDX is a **sibling app**, embedded rather than linked out to. The integration
is deliberately light:

1. **Embedded views** - when `HYPERDX_URL` is set on the dfe-ui deployment, the
   sidebar gains `/observe/*` entries (Search, Search list, Hunt Results) that
   iframe the chromeless dfe-hyperdx fork inside dfe-ui. The `(auth)` layout reads
   the variable server-side at request time, so it is a deployment setting, not a
   build-time one.
2. **"Create rule from a HyperDX search"** - HyperDX can send the browser a
   cross-window message (`postMessage`); dfe-ui catches it, stores the search, and
   opens its rule-create screen pre-filled. The UI checks the message came from the
   configured HyperDX origin.

So "auto-integration" today = **one environment variable** (`HYPERDX_URL`) plus a
postMessage handshake. The engine does NOT tell the UI where HyperDX is - the
deployment does. `NEXT_PUBLIC_HYPERDX_URL` is read by nothing and setting it does
nothing.

## 5. How it deploys

Every dfe-ui release publishes the image `ghcr.io/hyperi-io/dfe-ui` and the Helm chart
`oci://ghcr.io/hyperi-io/charts/dfe-ui` at the same version (1.7.12 as of 2026-10-11).
hyperi-ci renders the chart from dfe-ui's committed deployment contract,
`deploy/contract.json`.

- **Health.** `src/core/health.ts` serves scalo's probe contract on the app's one
  port, 3000: `/livez` answers 200 while the process runs, and `/readyz` answers 200
  until the pod is told to drain. Neither checks the engine, so an engine outage does
  not pull the UI from its Service. The startup probe reuses `/livez`. Both paths
  bypass the auth middleware (`src/proxy.ts`), so a probe never reads a login redirect
  as success.
- **NEXTAUTH_SECRET.** The contract declares it as a required secret (group
  `nextauth`, key `secret`), so the chart injects it into the pod's environment.
- **Engine address.** The image is built with `NEXT_PUBLIC_API_URL` empty, so the
  browser calls the engine on its own origin, and the server side reads
  `INTERNAL_API_URL` at request time. One image serves every environment.
- **HyperDX address.** `HYPERDX_URL`, or `HYPERDX_PORT` on the UI's own host, is read
  at request time (section 4).

The `NEXT_PUBLIC_*` values left are the session refresh timings and a display suffix.
Next.js still inlines those at build, so changing one needs a rebuild.

## 6. The config problem, explained simply

A Next.js quirk: public config (`NEXT_PUBLIC_*`) is **replaced with literal text
inside the compiled JavaScript at build time**. So you cannot change the API URL or
HyperDX URL of an already-built image - they are no longer variables, just baked-in
strings. Building a different image per environment is the anti-pattern everyone
warns against, which is why dfe-ui reads its engine and HyperDX addresses at runtime
(section 5).

## 7. Where we take it (the fixes + improvements)

Two tracks. Track A makes it deploy; Track B makes it better under Governed Ops.
Detailed tasks live in `docs/superpowers/plans/2026-06-30-governed-ops-api.md`
(Phase 4).

### Track A - make it deploy

Landed: health routes, `NEXTAUTH_SECRET` from the Secret, the published image and
chart, and runtime engine and HyperDX addresses (section 5).

### Track B - runtime config from the engine

The engine serves `GET /api/v1/config/client`: the API base (same origin), the
HyperDX URL and whether it is enabled, feature flags and the auth mode. dfe-ui shows
it on the System Management page but still takes its HyperDX address from its own
environment (section 4). Reading that address from the engine leaves one place to
set it.

### Track B - the UI becomes a Governed Ops window

Every CRUD screen (sources, schemas, rules, hunts, deployment dials) is the same
"edit YAML in git through the governed API" operation. So the UI gains, uniformly:

- a **current vs pending** view ("these changes are committed but not yet applied"),
- **conflict handling** (if someone else changed it, show current vs yours vs
  theirs - from the commit-SHA version token),
- **RBAC-driven visibility** (you only see/do what your class/action/operation
  grants allow).

These come "for free" because the engine exposes them once; the UI renders them.

### Track B - kill the hand-written boilerplate (developer experience)

Today UI devs hand-write a `useInfiniteQuery` wrapper per endpoint. Best practice
(2026) is to **generate** the typed hooks from the OpenAPI spec - publish a
`@dfe-engine/client` package (generated in CI when the spec changes) so the UI gets
`useListSources()`, `useCreateRule()`, etc. for free. Candidates: Orval (full hook
generation + can also generate mock data to test without a backend) or Hey API (the
modern `queryOptions()` pattern that composes well with Next.js server rendering).
Either removes the boilerplate and keeps UI and engine in lockstep with the spec.

### Track B - HyperDX: from "link" to genuinely integrated

- Serve the HyperDX URL + connection from the bootstrap endpoint (not a build-time
  env), so it is runtime + gitops-driven.
- Put dfe-ui and HyperDX behind the **same Envoy gateway with shared OIDC**, so
  there is one login and HyperDX feels part of the product (no separate sign-in).
- Optionally embed HyperDX views (iframe on a shared origin, or its component
  library from the fork) for the in-product, Kibana-like data exploration we want -
  a design choice to weigh against keeping it a separate tab.

### Track B - live updates

Lists are polled today. The engine already has Server-Sent Events for tasks; extend
that so long-running things (hunt runs, task progress, deploy reconcile status) push
to the UI instead of being polled.

## 8. References

- Next.js runtime env / single image multi-env: <https://nextjs.org/docs/app/getting-started/deploying> ; <https://nemanjamitic.com/blog/2025-12-13-nextjs-runtime-environment-variables/>
- Typed React Query from OpenAPI (Orval / Hey API / openapi-typescript): <https://orval.dev/docs/guides/react-query/> ; <https://www.saschb2b.com/blog/typesafe-api-codegen-2026>
- Engine API contract for the UI: [../control-plane/ui-api-guide.md](../control-plane/ui-api-guide.md)
- Governed Ops model: [../control-plane/governed-ops-design.md](../control-plane/governed-ops-design.md) and [../architecture.md](../architecture.md)
