<!-- Project: dfe-engine (cross-references dfe-ui, dfe-infra, hyperi-hyperdx) -->
# dfe-ui: how it works, how it deploys, how it talks to the engine

Written for someone who is NOT a front-end developer. It explains the moving
parts in plain terms, what is currently broken for deployment, and where we are
taking it - especially under the Governed Ops model.

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
- The engine publishes an **OpenAPI spec** (`dfe-engine/openapi-spec/openapi.json`)
  - a machine-readable description of every endpoint, its inputs and outputs.
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

HyperDX is a **sibling app**, not embedded. The integration is deliberately light:
1. **Sidebar links** - if the UI is built with `NEXT_PUBLIC_HYPERDX_URL` set, it
   adds Search / Chart Explorer / Dashboards links that open HyperDX in a new tab.
2. **"Create rule from a HyperDX search"** - HyperDX can send the browser a
   cross-window message (`postMessage`); dfe-ui catches it, stores the search, and
   opens its rule-create screen pre-filled. The UI checks the message came from the
   configured HyperDX origin.

So "auto-integration" today = **one environment variable** (`NEXT_PUBLIC_HYPERDX_URL`)
plus a postMessage handshake. There is no shared login session yet (new tab), no
embedded/iframe view, and the engine does NOT tell the UI where HyperDX is - the UI
learns it from a build-time env var.

## 5. How it deploys - and why it currently does NOT work

There IS a `Dockerfile` (dfe-ui) and a Helm chart (`dfe-infra/helm/charts/dfe-ui`),
but a deploy will not come up. The concrete blockers:

1. **No health endpoints.** The chart's liveness/readiness probes hit
   `/livez` and `/readyz`, which the app does not implement -> the pod
   never becomes Ready. (Fix: add two tiny Next.js route handlers.)
2. **NEXTAUTH_SECRET not wired.** The chart names a secret but the Deployment never
   mounts it -> JWT signing fails, logins break. (Fix: inject it as env from the
   Secret.)
3. **Image not published yet** to `ghcr.io/hyperi-io/dfe-ui` (CI publishes on
   release; not live yet).
4. **The big one - config is baked in at build time.** Anything named
   `NEXT_PUBLIC_*` (the API URL, the HyperDX URL) is **frozen into the JavaScript
   when the image is built**. That means one image only works for one environment,
   and setting those env vars on the running pod does nothing. This breaks
   "build once, deploy anywhere". (Fix: see section 7.)

## 6. The config problem, explained simply

A Next.js quirk: public config (`NEXT_PUBLIC_*`) is **replaced with literal text
inside the compiled JavaScript at build time**. So you cannot change the API URL or
HyperDX URL of an already-built image - they are no longer variables, just baked-in
strings. Building a different image per environment is the anti-pattern everyone
warns against. The fix is to make the UI read its config **at runtime** instead.

## 7. Where we take it (the fixes + improvements)

Two tracks. Track A makes it deploy; Track B makes it better under Governed Ops.
Detailed tasks live in `docs/superpowers/plans/2026-06-30-governed-ops-api.md`
(Phase 4).

### Track A - make it deploy (small, mechanical)
- Add `/livez` + `/readyz` route handlers.
- Mount `NEXTAUTH_SECRET` from the K8s Secret; add image-pull secret.
- Publish the image to ghcr.
- Switch off build-time `NEXT_PUBLIC_*` for the API/HyperDX URLs (next item).

### Track B - runtime config via a Governed Ops bootstrap endpoint (the keystone)
Add an engine endpoint `GET /api/v1/config/client` that returns, at runtime, what
the UI needs: the API base (same-origin), the HyperDX URL + whether it is enabled,
feature flags, and auth settings. The UI fetches this on startup instead of baking
it in. Result: **one image, every environment**, and - crucially - that client
config is itself **gitops-driven** (it comes from the engine reading YAML in git),
so it fits Governed Ops exactly. This single change fixes the deploy blocker AND
the multi-env story.

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
- Next.js runtime env / single image multi-env: https://nextjs.org/docs/app/getting-started/deploying ; https://nemanjamitic.com/blog/2025-12-13-nextjs-runtime-environment-variables/
- Typed React Query from OpenAPI (Orval / Hey API / openapi-typescript): https://orval.dev/docs/guides/react-query/ ; https://www.saschb2b.com/blog/typesafe-api-codegen-2026
- Engine API contract for the UI: [../control-plane/ui-api-guide.md](../control-plane/ui-api-guide.md)
- Governed Ops model: [../control-plane/governed-ops-design.md](../control-plane/governed-ops-design.md) and [../architecture.md](../architecture.md)
