# Contributing

We welcome contributions to this project. By contributing, you agree to the
terms outlined below.

## Commit Message Format

HyperI projects use [Conventional Commits](https://www.conventionalcommits.org/)
and [semantic-release](https://semantic-release.gitbook.io/) for automated
versioning and changelog generation. All commits must follow this format:

```text
<type>(<scope>): <subject>

[optional body]

[optional footer(s)]
```

### Types

| Type | Description | Version Bump |
|------|-------------|--------------|
| `feat` | A new feature | Minor (0.X.0) |
| `fix` | A bug fix | Patch (0.0.X) |
| `docs` | Documentation only | None |
| `style` | Code style (formatting, semicolons, etc.) | None |
| `refactor` | Code change that neither fixes a bug nor adds a feature | None |
| `perf` | Performance improvement | Patch (0.0.X) |
| `test` | Adding or correcting tests | None |
| `build` | Changes to build system or dependencies | None |
| `ci` | Changes to CI configuration | None |
| `chore` | Other changes that don't modify src or test files | None |
| `revert` | Reverts a previous commit | Varies |

### Breaking Changes

For breaking changes that require a major version bump, add `!` after the type
or include `BREAKING CHANGE:` in the footer:

```text
feat!: remove deprecated API endpoints

BREAKING CHANGE: The /v1/users endpoint has been removed. Use /v2/users instead.
```

### Examples

```text
feat(auth): add OAuth2 support for Google login

fix(api): handle null response from upstream service

docs: update installation instructions for Windows

refactor(core)!: restructure module exports

BREAKING CHANGE: Named exports are now used instead of default exports.
```

### Scope

Scope is optional but recommended. Use it to indicate the area of the codebase
affected (e.g., `api`, `auth`, `core`, `cli`, `docs`).

## Semantic Versioning

This project follows [Semantic Versioning 2.0.0](https://semver.org/):

- **MAJOR** (X.0.0): Breaking changes that require users to modify their code
- **MINOR** (0.X.0): New features that are backwards-compatible
- **PATCH** (0.0.X): Bug fixes and minor improvements

Versions are automatically determined by semantic-release based on commit
messages. Do not manually update version numbers.

## Developer Certificate of Origin

This project uses the Developer Certificate of Origin (DCO) to ensure that
contributors have the right to submit their contributions.

By making a contribution to this project, you certify that:

1. The contribution was created in whole or in part by you and you have the
   right to submit it under the license indicated in the file; or

2. The contribution is based upon previous work that, to the best of your
   knowledge, is covered under an appropriate open source license and you
   have the right under that license to submit that work with modifications;
   or

3. The contribution was provided directly to me by some other person who
   certified (1), (2) or (3) and you have not modified it.

4. You understand and agree that this project and the contribution are public
   and that a record of the contribution (including all personal information
   you submit with it, including your sign-off) is maintained indefinitely
   and may be redistributed consistent with this project or the license(s)
   involved.

## How to Sign Off Your Commits

You must sign off each commit to indicate your acceptance of the DCO. Combine
the signoff with your conventional commit message:

```bash
git commit --signoff -m "feat(auth): add two-factor authentication"
```

This produces:

```text
feat(auth): add two-factor authentication

Signed-off-by: Your Name <your.email@example.com>
```

Make sure your Git configuration has your correct name and email:

```bash
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"
```

## License for Contributions

All contributions to this project are licensed under the Business Source
License 1.1 (BUSL-1.1), the same license that covers the project.

Each version of the software (including your contributions) will automatically
become available under the Apache License, Version 2.0 on the third
anniversary of its release.

## Running locally

To run the engine as a plain local process (a `uv` virtualenv, no containers or
Kubernetes) -- including the no-dependencies auth spike and how it wires to
dfe-hyperdx / dfe-ui / a local dfe-deploy clone -- see [docs/local-dev.md](docs/local-dev.md).

## How to Contribute

1. **Fork the repository** and create your branch from `main`
2. **Make your changes** following the commit message format above
3. **Sign off your commits** with the DCO
4. **Test your changes** to ensure they work as expected
5. **Submit a pull request** with a clear description of what you've done

### Pull Request Checklist

- [ ] Commits follow the conventional commit format
- [ ] All commits are signed off (DCO)
- [ ] Tests pass (if applicable)
- [ ] Documentation is updated (if applicable)

### Stacked pull requests

Open every pull request against `main`. `.github/workflows/ci.yml` fires `pull_request` only for a base of `main`, so a PR based on another branch gets the branch `push` run instead, which plans `run-checks=false`. Quality, Test, Build and Commit messages then all report `skipped`, the run still concludes `success`, and a skipped required check counts as satisfied - the PR reads green with nothing run. The `PR base` check from `.github/workflows/pr-base-guard.yml` fails on those PRs so the state is visible rather than silent.

If you stack anyway, two things will bite:

- Retarget every child to `main` BEFORE merging its parent, then push the child so the `pull_request` run fires. Merging a parent with `--delete-branch` deletes the child's base and GitHub closes the child. The two states deadlock: you cannot reopen a PR whose base branch is gone, and you cannot retarget a closed PR. Recovery is to recreate the base ref at any commit, reopen, retarget to `main`, then delete the ref again.
- A squash merge rewrites the history the child sits on, so replay only the child's own commits: `git rebase --onto main <old-base-head> <child-branch>`.

## Testing your branch in a full stack

DFE is a suite of several repos (engine, infra, schemas, ui, hyperdx, the Rust
data-plane apps). A change that spans more than one of them, or one you want to
see running, gets a full stack: on one host with
[dfe-docker](https://github.com/hyperi-io/dfe-docker), or on a Kubernetes cluster
with [dfe-infra](https://github.com/hyperi-io/dfe-infra/blob/main/docs/deployment/README.md).
Both take the image tag or branch you nominate for each app. Where that stack
runs is your own concern and is never committed here.

## Run the checks locally before you push

HyperI projects gate every push through CI (lint, format, tests, secret scan,
dependency audit, build). Run the SAME checks locally first so your change lands
green instead of bouncing:

```bash
hyperi-ci check
```

`hyperi-ci` is the HyperI CI CLI (public on PyPI). If you do not have it, install
it once with `uv tool install hyperi-ci` (or `pipx install hyperi-ci`), or run it
ad hoc with `uvx hyperi-ci check`. It runs the project's full local validation --
the same suite CI runs -- and reports what to fix. Run it before every push and before opening a
pull request. It is the single best way to give your change the best chance of
surviving CI.

## CI/CD Workflow

When your pull request is merged to `main`:

1. **semantic-release** analyses commit messages since the last release
2. Determines the next version number based on commit types
3. Generates/updates the CHANGELOG
4. Creates a new GitHub release with release notes
5. Publishes the package (if applicable)

This happens automatically - no manual intervention required.

## Questions

If you have questions about contributing, please open an issue or contact us.

## Context

### What this is

The control plane for a DFE deployment: a Python library plus an API server that
owns configuration, schema and app lifecycle. It is NOT the data path -- records
never flow through here. They go receiver -> Kafka or gRPC -> loader ->
ClickHouse, and this engine decides what those components are configured to do.

System design and why it is built this way:
[docs/architecture.md](docs/architecture.md).

### Where things live

| path | what |
|---|---|
| `src/dfe_engine/api/` | FastAPI routers, four auth paths, RBAC |
| `src/dfe_engine/appmgmt/` | one generic per-app surface, and the catalogue is DATA |
| `src/dfe_engine/schema/` | ClickHouse DDL planning and apply |
| `src/dfe_engine/source/` | Source definitions. Receiver and loader routing is COMPILED from these |
| `src/dfe_engine/gitops/` | every mutation is a git commit, and the deploy repo is the authority |
| `docs/superpowers/` | specs and plans, including designs that are NOT built yet |

### Commands that prove a change

```bash
uv sync                       # deps, from the committed uv.lock
uv run pytest                 # the suite
uv run ruff check src tests   # lint
hyperi-ci check --quick       # what CI will say, faster
```

`ruff format` is available but the repo has not adopted it, and there is no `black`.

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
| Treat this as an internal tool | Code to the injected config and interface | It is a PRODUCT other organisations deploy. Env-specifics -- cluster refs, cred paths, our fleet -- belong in private config repos, never here. |
| Hand-edit compiled routing | Change the Source | Receiver and loader routing is derived, so the API reports a hand edit as DRIFT and re-syncs over it. |
| Mock a backing service | testcontainers, a real one | No mocks as proof. `pass` and `TODO` are not functionality. |
| `from typing import List, Dict, Optional` | `list[str]`, `str \| None` | Built-in generics, and `requires-python = ">=3.14"`. |
| `datetime.utcnow()` | `datetime.now(UTC)` | Deprecated since 3.12. |
| stdlib `logging` | `from scalo.logger import logger` | House rule, so every service emits one structured JSON format. |
| Raw `httpx` with hand-rolled retry | `scalo.http.HttpClient` | House rule. Importing httpx for its exception and response TYPES is correct and expected. |
| `yaml.safe_load` | `dfe_engine.yaml_utils` | ruamel, YAML 1.2. YAML 1.1 reads `no` as boolean false. |
| `except Exception:` then a silent fallback | Catch the specific error, log with context, re-raise | 12 sites already return `False`/`None`/`[]` with no trace, out of 251 `except Exception:` sites in `src/`. An operator cannot tell failure from empty. |
| Assume a config key does something | Grep for its consumer | Config that parses and is never read is a recurring defect here. |
| Trust `gh issue list` | `gh issue view <n>` | It caps at 30 without `--limit`, so absence is not evidence of closure. |

Two traps that are not style:

**`rg` and `fd` respect `.gitignore`**, so `.env` and `CLAUDE.md` are invisible
to a default search. Pass `--no-ignore` or you will conclude they do not exist.

**Every container this repo starts comes down the same session.** Stray
containers caused a host OOM. Only ever stop what you started.

### Where this sits

Membership and every edge below are declared in `dfe-infra/suite.yaml`. Read it
rather than trusting this table, and regenerate any claim from
`dfe-stack suite --consumer dfe-engine` (inbound) or `--producer dfe-engine`
(outbound). Both print the interaction kind and what to re-run when the edge
moves.

**What dfe-engine depends on:**

| repo | how it interacts |
|---|---|
| dfe-infra | Deploys this engine, and owns `apps.yaml` -- a byte copy is vendored here at `appmgmt/apps.yaml` as the last resort in the resolution order. Lockstep: a catalogue entry the engine does not carry is not reflected by its management API. |
| dfe-schemas | Ships the schema and DDL trees as a PACKAGE, not a submodule. Read out of the installed wheel unless `DFE_ENGINE_SCHEMAS_DIR` points elsewhere. |
| scalo-py | The shared Python library, plus a contract guard: this repo overrides scalo's runtime base image with its own literal, and its own test fails when the committed Dockerfile stops matching. |
| the Rust apps | Each emits its config schema (`<app> config-schema`), and the deployment mounts it at `DFE_ENGINE_APP_CONTRACT_DIR`. `/api/v1/services` takes every default and type from it, so nothing of an app's config is restated here. |
| logreducer | A declared dependency (`logreducer[enhanced]`). The sampler's `smart` mode, its default, and `anomaly` mode run it, imported lazily so the scikit-learn stack loads only when one of them does. |

**What depends on dfe-engine:**

| repo | how it interacts |
|---|---|
| dfe-infra | Pins this engine's image in TWO places: the dfe-engine chart and the hyperdx chart, which runs the engine image as its dashboards init container. Both move together. There is no schema-apply workload, because the engine applies schema at its own startup. |
| dfe-ui | Vendors this repo's OpenAPI spec and generates its scope types from an engine module. A workflow here re-vendors it by PR on a push to main. |

**The consequence, and it is the reason this section exists:** dfe-engine is the
ONLY controller of ClickHouse objects and Kafka topics. A change to schema,
source compilation or topic naming is a SUITE-WIDE move, not a local one --
every Rust service reads configuration this engine writes, and infra and docker
wait on it. A one-line change here can land in five other repositories.
