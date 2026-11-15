<!--
  Project:   dfe-engine
  File:      docs/control-plane/dfe-cli-design.md
  Purpose:   How the `dfe` client CLI is built and how it resolves profiles
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# The `dfe` CLI

`dfe` is the engine's HTTP client. Its command tree is generated from the engine's OpenAPI document, so an endpoint becomes a command with no CLI code. It is scalo's `SpecCli` (`src/dfe_engine/cli/auto/app.py`), modelled on the AWS and gcloud CLIs, and ships as the `dfe` console script in this package (`pyproject.toml`, `[project.scripts]`).

`dfe-engine` is the server. The two are separate entry points: `dfe` talks to an engine over HTTP, and its `local` group works on the deploy repo when no engine is running.

## The command tree

- One group per API tag, one command per operation: `dfe hunts list | get | create | apply | delete | run | status`.
- Path parameters are positional arguments, in path order. Body fields become flags, and `--cli-input-json`, `--cli-input-yaml` and `--generate-cli-skeleton` take or print the whole body.
- The tree is read from the installed engine package's own app factory (`load_live_spec`), so it matches the engine version installed beside it. An endpoint opts out with `x-scalo.cli.hidden`.
- Commands with no operation behind them are mounted on top: `login`, `logout`, `config`, `auth list`, `local`, `schema` and `kafka client-config`.

## Profiles and credentials

Profiles live under `~/.config/dfe` (`DFE_ENGINE_CONFIG_HOME` moves the root): an INI file per profile in `configurations/` holding `url` and `account`, the active profile's name in `active_config`, and `credentials.json` (mode 0600) keyed by account. Resolution, first wins:

| Setting | Order |
|---|---|
| Endpoint | `--endpoint-url`, `DFE_ENGINE_API_URL`, the profile's `url` |
| Credential | `DFE_ENGINE_API_KEY`, `DFE_ENGINE_API_TOKEN`, the stored credential of the profile's account |
| Profile | `--profile`, `DFE_ENGINE_PROFILE`, `active_config` |

`dfe login` stores an API key (`--api-key`), or exchanges `--username` and `--password` for a JWT through `POST /api/v1/auth/login`. `dfe config set | get | list | configurations` reads and writes profiles.

## Global options

- `--output json|yaml|table|text` (`DFE_ENGINE_OUTPUT`). The default is `json` off a terminal, else `yaml` for a get and `table` for a list.
- `--query <JMESPath>` projects the answer before it is printed.
- A list follows `next_page_token` to the end. `--no-paginate` fetches one page.
- `--verbose`, `--quiet`, `--debug`, `--yes` (skip a delete's confirmation) and `--no-input` (never prompt).

## Break-glass: `dfe local`

`dfe local` reads and writes a clone of the deploy repo through GitCrud with no daemon: `classes`, `ls`, `grep`, `get`, `log`, `versions`, `status`, `set`, `unset`, `rm`, `revert`, `restore` and `push`, plus `ch-cloud` for a ClickHouse Cloud service. `dfe local log` flags the commits made this way.

## Not built

A frozen single binary, a Homebrew tap, a container image and a browser OIDC login are not built.
