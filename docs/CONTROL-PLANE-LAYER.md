# The DFE Control-Plane Layer

The **control-plane layer** is the small set of repos that own the DFE control
plane - configuration, RBAC, schema, the operator UI, and the deploy target.
When a doc says "the control-plane layer" it means exactly these repos. Anything
that interacts directly, or one level removed, with the dfe-engine API or what
it does is reasoning about this layer.

## The layer

```mermaid
flowchart TB
    subgraph CP["Control-plane layer"]
        engine["dfe-engine<br/>config control plane + sole authz PDP<br/>the FastAPI API + the dfe CLI"]
        schemas["dfe-schemas<br/>schema SSoT (event header, DDL, profiles)"]
        ui["dfe-ui<br/>operator console (window over the API)"]
        infra["dfe-infra<br/>GitOps SSoT + deploy vehicle (Argo CD)"]
        deploy[("dfe-deploy<br/>per-deployment RW gitops repo<br/>we own only the INITIAL state")]
    end

    ui -->|HTTP, engine-minted JWT| engine
    engine -->|reads| schemas
    engine -->|commits config| deploy
    infra -->|deploys + syncs| deploy
    ai(["dfe-ai<br/>peer service - NOT control-plane"]) -.->|calls the engine API| engine

    classDef cp fill:#cfe2ff,stroke:#084298;
    classDef ext fill:#e2e3e5,stroke:#41464b;
    class engine,schemas,ui,infra cp;
    class deploy,ai ext;
```

| Repo | Role in the layer |
|---|---|
| **dfe-engine** | The config control plane + the sole authorisation PDP. Hosts the FastAPI API and the generated `dfe` CLI. Everything else in the layer keys off its API. |
| **dfe-schemas** | The schema SSoT (common event header, hunt-results DDL, profiles). The engine reads it (git submodule) and auto-creates from it. |
| **dfe-ui** | The operator console. A window over the engine API - it holds no authority of its own; it verifies the engine-minted JWT. |
| **dfe-infra** | The GitOps SSoT and deploy vehicle (Argo CD). Deploys the suite; never a config authority. |
| **dfe-deploy** | The per-deployment read-write gitops repo the running engine commits to. We own only its **initial** state; after handover the deployment owns it. |

## dfe-engine, never "dfe-api"

The API is **the dfe-engine API**. `dfe-api` was the old server entry-point name
and is retired: the daemon binary is now `dfe-engine` (`dfe-engine run`), the
human CLI is `dfe`. Anything talking to the API is talking to the dfe-engine API
- do not call it "the dfe-api". (The `DFE_API_*` env prefix stays, purely to keep
settings resolution and the Helm chart stable.)

## openapi.json is the API SSoT

`openapi-spec/openapi.json` is the **single source of truth for the API surface**.
It is generated from the FastAPI app (`uv run python openapi-spec/generate.py`)
and committed. Every consumer keys off it, never off a hand-maintained copy:

The spec is the hub; everything radiates out of it. The FastAPI routes feed it,
and every downstream consumer keys off it - never off a hand-kept copy:

```mermaid
flowchart TB
    routes["FastAPI routes<br/>(the live API definition)"]
    routes -->|"openapi-spec/generate.py"| spec[("openapi.json<br/>THE API SSoT")]

    spec -->|"command tree at runtime<br/>(x-cli:false opts out)"| cli["dfe CLI"]
    spec -->|"openapi-typescript<br/>-> @dfe/dfe-engine-types"| ui["dfe-ui"]
    spec -->|"HTTP client + MCP tool schemas"| ai["dfe-ai peer service"]
    spec -->|"Prism"| mock["contract mock (dev + CI)"]
    spec -->|"Swagger UI"| refdocs["API reference docs"]

    classDef ssot fill:#fff3cd,stroke:#664d03,stroke-width:3px;
    classDef src fill:#e2e3e5,stroke:#41464b;
    class spec ssot;
    class routes src;
```

- The **`dfe` CLI** generates its whole command tree from the spec at runtime -
  add an endpoint and the CLI grows a command, with no CLI code. An endpoint
  opts out via the `x-cli` extension.
- **dfe-ui** generates its TypeScript types from the same spec.
- **dfe-ai** (the peer service) reads the spec for its client + MCP tool schemas.

So when the API changes: change the routes, regenerate `openapi.json`, and every
consumer follows. Never edit `openapi.json` by hand or let a consumer drift from
it.

## What is NOT in this layer

- **dfe-ai** is a **peer service**, not control-plane - a single modular service
  (all AI capability + an MCP layer in one deployable, sharing the scalo
  foundation with the engine). It is a compute peer the control plane
  orchestrates; it owns no config and calls the engine API for data. See the
  dfe-ai service spec.
- The data-plane repos (receivers, loaders, transforms, fetchers, archiver,
  hunt-runner) are downstream services the control plane configures - not part
  of it.
