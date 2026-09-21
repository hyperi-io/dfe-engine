# Contributing

We welcome contributions to this project. By contributing, you agree to the
terms outlined below.

## Commit Message Format

HyperI projects use [Conventional Commits](https://www.conventionalcommits.org/)
and [semantic-release](https://semantic-release.gitbook.io/) for automated
versioning and changelog generation. All commits must follow this format:

```
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

```
feat!: remove deprecated API endpoints

BREAKING CHANGE: The /v1/users endpoint has been removed. Use /v2/users instead.
```

### Examples

```
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

```
git commit --signoff -m "feat(auth): add two-factor authentication"
```

This produces:

```
feat(auth): add two-factor authentication

Signed-off-by: Your Name <your.email@example.com>
```

Make sure your Git configuration has your correct name and email:

```
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
dfe-hyperdx / dfe-ui / a local dfe-deploy clone -- see [docs/LOCAL-DEV.md](docs/LOCAL-DEV.md).

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

## Testing your branch in a full stack (branch previews)

> Status: in development. This section describes the intended contributor
> workflow; the tooling is being built (see dfe-infra
> `docs/plans/2026-07-08-branch-preview-cycle.md`).

DFE is a product suite of several repos (engine, infra, schemas, ui, hyperdx, the
Rust data-plane apps). To test a change that spans more than one repo, or to
eyeball a running stack, you deploy a **preview**: a full DFE stack built from the
branches you nominate.

- **Local by default.** A preview runs on a local `k3d`/`helm` cluster on your own
  machine - no cloud account and no access to anyone's infrastructure required. An
  external contributor can build and run a preview entirely on a laptop.
- **One manifest describes it.** A preview is a `DeployContext` (see the schema in
  the plan above): which branch/ref of each repo, a footprint profile, and where
  it lands. The same object drives a local preview and a production install.
- **Deploy tooling lives in dfe-infra** (the deploy layer). An org that operates a
  shared cluster can point the same tooling at it instead of local `k3d`; that
  configuration is that org's own private concern, never committed here.

## Run the checks locally before you push

HyperI projects gate every push through CI (lint, format, tests, secret scan,
dependency audit, build). Run the SAME checks locally first so your change lands
green instead of bouncing:

```
hyperi-ci check
```

`hyperi-ci` is the HyperI CI CLI (public on PyPI). If you do not have it, install
it once with `uv tool install hyperi-ci` (or `pipx install hyperi-ci`), or run it
ad hoc with `uvx hyperi-ci check`. It runs the project's full local validation --
the same suite CI runs -- and reports what to fix. Run it before every push and before opening a
pull request; it is the single best way to give your change the best chance of
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
