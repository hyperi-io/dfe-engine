<!--
  Project:   dfe-engine
  File:      docs/control-plane/dfe-cli-design.md
  Purpose:   Design for the `dfe` client CLI, modelled on the AWS CLI v2
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# `dfe` CLI - design (AWS CLI v2 model)

The goal: a `dfe` command that installs and behaves like the AWS CLI v2 - a
self-contained binary, `dfe configure` profiles, a stable auth-resolution chain,
and a noun-verb command tree that is DATA-DRIVEN from the API's own contract. This
is the CLIENT half; it speaks HTTP to a deployed dfe-engine, exactly as `aws` speaks
to AWS service endpoints.

Reference: the AWS CLI v2 source is cloned to `/Volumes/projects/aws-cli` (the
bundled-installer machinery under `backends/`/`exe/`/`macpkg/`/`docker/`, and the
`aws configure` family under `awscli/customizations/configure/`).

## What exists today (do not confuse the two)

- **`dfe-engine`** - the SERVER entry point (`pyproject [project.scripts]` -> uvicorn).
  It also carries a server-side, break-glass CLI (`dfe local governed ...`,
  `cli/governed_ops.py`) that calls the SAME GitCrud/services the API routers call -
  the standing "CLI is a wrapper over the API" rule. It runs WHERE the engine libs
  and the gitops repo live (CI, or on the engine host when the API is down).
- **`dfe`** - the remote HTTP client this document designed. It now ships
  from this repo (`cli/auto/`, click-based, generated from the OpenAPI spec).

Keep them distinct binaries. `dfe` talks to an engine over HTTP; `dfe-engine` IS the
engine (and its in-process break-glass path). Everything below is `dfe`.

## 1. Distribution and install (like AWS CLI v2, NOT pip)

AWS CLI v2 deliberately does not install via pip - it ships a self-contained bundle
(a frozen Python + all deps) so it never collides with a user's Python. Mirror that:

- **Frozen single binary** built with PyInstaller (AWS v2 uses the same approach;
  `backends/build_system` + `exe/`). One `dfe` executable per OS/arch, no Python
  required on the target.
- **Channels:**
  - GitHub Releases: per-OS/arch archive + `install` script
    (`curl -sSL https://.../install.sh | sh`), like the AWS v2 Linux zip installer.
  - Homebrew tap: `brew install hyperi-io/tap/dfe`.
  - Container: `ghcr.io/hyperi-io/dfe` (for CI - `docker run ... hyperi-io/dfe ...`).
  - `.pkg` (macOS) / `.msi` (Windows) later, as AWS v2 does (`macpkg/`).
- **Supply chain:** artifacts SHA-pinned and signed; the install script verifies the
  digest (matches the repo's pinning standard, docs/deployment/supply-chain-pinning.md).
- **Versioning:** `dfe --version` reports the CLI version AND the negotiated API
  version. The CLI is installed independently of the engine but declares a
  compatible API range; on mismatch it warns (older CLI vs newer engine is fine
  because the command tree is regenerated - see section 4).
- **Self-update:** `dfe --version` checks the release channel; `dfe upgrade` pulls
  the latest signed binary (optional, later).

For local development the same code is also exposed as a `dfe` console-script in
`pyproject`, so contributors run it from the venv without the frozen build.

## 2. Configuration and profiles (like ~/.aws/config)

Two files under `~/.dfe/` (env override `DFE_CONFIG_DIR`), splitting config from
secrets exactly as AWS v2 splits `config` and `credentials`:

```
~/.dfe/config
  [default]
  endpoint_url = https://dfe.example.com
  output       = json
  org          = acme            # default org/tenant scope (the DFE analogue of region)

  [profile prod]
  endpoint_url = https://dfe.prod.internal
  output       = table

~/.dfe/credentials              # 0600, never logged
  [default]
  api_key = dfe_...             # or nothing here if using `dfe login` (OIDC/JWT)
```

- **Selection:** `--profile prod` > `DFE_PROFILE` env > `[default]`.
- **`dfe configure`** - interactive first-run setup (prompts endpoint, auth, output).
- **`dfe configure set/get/list/list-profiles`** - the AWS v2 subcommand family
  (`customizations/configure/`), same verbs.
- **`dfe configure oidc`** - set up the OIDC login flow for a profile (the analogue
  of `aws configure sso`).

## 3. Authentication resolution chain (like the AWS credential provider chain)

The engine already has four auth paths (OIDC headers, API key, JWT Bearer,
disabled - `api/deps.py`). The CLI resolves a bearer credential in this fixed order,
first match wins:

1. Explicit flag: `--token <jwt>` or `--api-key <key>`.
2. Env: `DFE_TOKEN` / `DFE_API_KEY`.
3. A cached OIDC/JWT session from `dfe login` (`~/.dfe/cache/<profile>.json`,
   auto-refreshed while valid).
4. Profile credential in `~/.dfe/credentials` (`api_key`).
5. In-cluster: a mounted service-account/JWT token (for CI/Jobs), if present.

- **`dfe login`** - the browser OIDC flow (device-code or loopback redirect), the
  analogue of `aws sso login`; or `dfe login --username u` for the local-auth
  JWT path (`POST /api/v1/auth/login`). The resulting JWT is cached and refreshed.
- **`dfe logout`** - clears the cached session.
- 401 from the API -> the CLI prints "run `dfe login`" (or "check `--api-key`"),
  never a raw stack trace.

## 4. The command tree is DATA-DRIVEN from OpenAPI (the core alignment)

AWS CLI v2's entire command tree is generated from botocore's JSON service models -
that is what makes it consistent and always in step with the services. DFE already
ships the equivalent: `openapi-spec/openapi.json` (74 operations, 23 routers). The
`dfe` command tree is GENERATED from it:

- **router/tag -> noun group**, **operation -> verb**. So:
  - `dfe hunts list | get | create | update | delete | run`
  - `dfe rules list | create | validate | publish | rollback | versions`
  - `dfe helmvars get | set | list` (Tier-1 dials)
  - `dfe deployments get | publish | history`
  - `dfe queries run | views list|get|execute`
  - `dfe accounts | groups | roles | actions | policies ...` (governance)
  - `dfe sources | schemas | fieldmaps | sigma | discovery ...`
  - `dfe system health | version`
- **Path/query params -> options** (`--name`, `--page`, `--search`); **request-body
  schema -> flags**, plus `--cli-input-json` / `--cli-input-yaml` and
  `--generate-cli-skeleton` for the whole body (AWS v2 verbs, invaluable for CRUD
  of a large YAML resource).
- **Responses -> the output formatter** (section 5).
- Regenerated whenever the API changes, so the CLI cannot drift from the contract.
  An older CLI against a newer engine still works for the operations it knows; new
  operations appear when the CLI is regenerated/updated.

Implementation: a small generator reads `openapi.json` and emits a click tree
(`cli/auto/build.py`). The HTTP layer MUST be scalo's
`HttpClient`/`AsyncHttpClient` (library policy - never raw httpx). Auth, retries,
pagination, and output live in the shared client core; the generated tree is thin.

## 5. Global options (mirror the AWS CLI)

Available on every command:

- `--profile`, `--endpoint-url`, `--region`-analogue `--org`.
- `--output {json,yaml,table,text}` - `json` default (scripts), `table` for humans.
- `--query <JMESPath>` - client-side projection (reuse the `jmespath` lib AWS uses).
- `--no-paginate` / `--page-size N` / `--max-items N`.
- `--debug` (wire log), `--no-verify-ssl`, `--cli-read-timeout`, `--cli-connect-timeout`.
- `--cli-input-json|yaml`, `--generate-cli-skeleton`.

## 6. Pagination

The API's list endpoints return `PaginatedResponse[T]` with a computed `next_page`.
The CLI auto-paginates by following `next_page` until exhausted (AWS v2 default),
assembling one result set; `--no-paginate` returns a single page, `--max-items`
caps, `--page-size` sets the server page size.

## 7. Governed-Ops semantics (DFE-specific, beyond AWS)

Every mutation in DFE is a git commit over the gitops repo, so the CLI exposes what
AWS has no analogue for:

- `--message "<why>"` on writes -> the commit message (falls back to a sensible
  default); the CLI prints the resulting revision SHA.
- `--dry-run` -> validate + show the diff without committing (maps to the API's
  validate path).
- Optimistic concurrency: `--if-match <revision>` sends the base revision; on a 409
  the CLI renders the 3-way view (current-at-head vs yours vs theirs) the
  `ConcurrencyConflictError` already carries, instead of a bare error.
- Versioned classes (rules/hunts, via VersionedDoc): the lifecycle verbs
  `publish` / `rollback` / `versions` / `diff` map to draft/publish/rollback. A dial
  class (helmvars) has no such verbs (it is unversioned) - the generator omits them,
  matching the `versioned` opt-in.

## 8. Ergonomics (AWS CLI v2 features to match)

- **`--cli-auto-prompt`** - interactive prompting for required args (AWS v2 flagship
  feature); `dfe` with no subcommand can drop into it.
- **Shell completion** - `dfe completion bash|zsh|fish` emits a completer.
- **Help system** - `dfe help`, `dfe <noun> help`, `dfe <noun> <verb> help`,
  generated from the OpenAPI summaries/descriptions.
- **Non-zero exit codes** by error class (auth, not-found, conflict, validation,
  server) so scripts can branch.

## 9. Build/rollout sketch (phased)

1. **Client core** - config/profile loader, the auth chain + `dfe login`, the scalo
   HTTP client, output formatters (json/yaml/table/text + JMESPath), pagination.
2. **Generator** - `openapi.json` -> click tree; wire a first vertical slice
   (`dfe hunts`, `dfe helmvars`, `dfe deployments`) end to end.
3. **Governed-ops verbs** - `--message`/`--dry-run`/`--if-match`, the versioned
   lifecycle verbs, the 409 3-way renderer.
4. **Packaging** - PyInstaller frozen build in CI (per OS/arch), `install.sh`,
   Homebrew tap, container image; sign + SHA-pin the artifacts.
5. **Ergonomics** - auto-prompt, completion, `dfe configure` family, self-update.

Each phase ships a usable CLI; the generator (phase 2) is the keystone that keeps
`dfe` in lockstep with the engine's contract the way botocore models keep `aws` in
lockstep with AWS.
