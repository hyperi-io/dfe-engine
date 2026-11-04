#  Project:      dfe-engine
#  File:         cli/auto/authcmds.py
#  Purpose:      Hand-written login/logout/auth/config commands over scalo's profile store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The credential and configuration commands that have no OpenAPI operation.

``login`` stores the credential the later commands send. ``auth list`` and
``auth print-access-token`` sit beside the generated ``auth`` operations, and
``config`` reads and writes the profile files.
"""

from typing import Annotated

import typer
from scalo.cli.openapi import Credential, Invocation, SpecCli, invocation
from scalo.cli.openapi.client import Client
from scalo.cli.openapi.errors import ApiError, CliConfigError
from scalo.cli.openapi.transport import Response, TransportError


def _send(
    run: Invocation,
    url: str,
    method: str,
    path: str,
    credential: Credential | None = None,
    body: object = None,
) -> Response:
    """One request to ``url``, raising ``ApiError`` on a non-2xx answer."""
    cli = run.cli
    client = Client(
        url, credential, cli.transport, api_key_header=cli.api_key_header, timeout=cli.timeout
    )
    reply = client.send(method, path, body=body)
    if not reply.ok:
        raise ApiError(reply)
    return reply


def _whoami(run: Invocation, url: str, api_key: str) -> str | None:
    """The account an API key belongs to, ``None`` when the engine will not say."""
    try:
        reply = _send(run, url, "GET", "/api/v1/auth/me", Credential(account="?", api_key=api_key))
        return (reply.json() or {}).get("user_id")
    except ApiError, TransportError, ValueError:
        return None


def login(
    url: Annotated[
        str | None, typer.Option(help="Engine base URL, e.g. https://engine.example:8000")
    ] = None,
    api_key: Annotated[str | None, typer.Option(help="Authenticate with a static API key.")] = None,
    username: Annotated[
        str | None, typer.Option("--username", "-u", help="Username for password login.")
    ] = None,
    password: Annotated[
        str | None, typer.Option("--password", "-p", help="Password for password login.")
    ] = None,
    profile: Annotated[
        str | None, typer.Option(help="Named configuration (profile) to write to.")
    ] = None,
) -> None:
    """Authenticate and store credentials for later commands.

    Either pass --api-key, or --username/--password to exchange for a JWT. With no
    new credentials but a valid stored one, just re-activates the profile.
    """
    run = invocation()
    store = run.store
    name = profile or run.profile or store.active_profile()
    if url:
        store.set_value("url", url, name)
    target = url or store.get_value("url", name)
    if not target:
        raise CliConfigError("--url is required for the first login to a profile.")

    credential: Credential | None = None
    if api_key:
        account = _whoami(run, target, api_key) or username or "api-key"
        credential = Credential(account=account, api_key=api_key)
    elif username and password:
        reply = _send(
            run,
            target,
            "POST",
            "/api/v1/auth/login",
            body={"username": username, "password": password},
        )
        account = username
        credential = Credential(account=account, token=reply.json()["access_token"])
    else:
        account = store.get_value("account", name)
        if not account or store.get_credential(account) is None:
            raise CliConfigError(
                "No stored credentials for this profile. Pass --api-key or --username/--password."
            )

    if credential is not None:
        store.save_credential(credential)
        store.set_value("account", credential.account, name)
    store.set_active_profile(name)
    typer.echo(f"You are now logged in as {account}. Active endpoint: {target}")


def logout(
    profile: Annotated[
        str | None, typer.Option(help="Named configuration (profile) to clear.")
    ] = None,
) -> None:
    """Clear the stored credential + active account for a profile."""
    run = invocation()
    name = profile or run.profile or run.store.active_profile()
    account = run.store.get_value("account", name)
    if account:
        run.store.delete_credential(account)
        run.store.set_value("account", "", name)
        typer.echo(f"Logged out {account}.")
    else:
        typer.echo("No active account to log out.")
    auth_list()


def auth_list() -> None:
    """List configured accounts, marking the active one."""
    store = invocation().store
    active = store.get_value("account", store.active_profile())
    accounts = store.list_accounts()
    if not accounts:
        typer.echo("No credentials stored. Run `dfe login ...`.")
    for account in accounts:
        typer.echo(f"{account}{' (active)' if account == active else ''}")


def print_access_token() -> None:
    """Print the active token to stdout (for scripts)."""
    run = invocation()
    account = run.store.get_value("account", run.profile or run.store.active_profile())
    if not account:
        raise CliConfigError("No active account.")
    credential = run.store.get_credential(account)
    if credential is None or credential.value is None:
        raise CliConfigError("No stored token for the active account.")
    typer.echo(credential.value)


config_app = typer.Typer(
    help="Read and write CLI configuration (INI-backed profiles).", no_args_is_help=True
)
configurations_app = typer.Typer(
    help="Manage named configurations (profiles).", no_args_is_help=True
)
config_app.add_typer(configurations_app, name="configurations")


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a config KEY to VALUE in the active profile."""
    invocation().store.set_value(key, value)
    typer.echo(f"Set {key}.")


@config_app.command("get")
def config_get(key: str) -> None:
    """Print a single config KEY from the active profile."""
    value = invocation().store.get_value(key)
    if value is None:
        invocation().fail(CliConfigError(f"'{key}' is not set."))
    typer.echo(value)


@config_app.command("list")
def config_list() -> None:
    """Print all config keys for the active profile."""
    store = invocation().store
    typer.echo(f"[{store.active_profile()}]")
    for key, value in store.profile_items().items():
        typer.echo(f"{key} = {value}")


@configurations_app.command("list")
def configurations_list() -> None:
    """List named configurations, marking the active one."""
    store = invocation().store
    active = store.active_profile()
    for name in store.list_profiles() or [active]:
        typer.echo(f"{name}{' (active)' if name == active else ''}")


@configurations_app.command("activate")
def configurations_activate(name: str) -> None:
    """Make NAME the active configuration."""
    invocation().store.set_active_profile(name)
    typer.echo(f"Activated configuration [{name}].")


@configurations_app.command("create")
def configurations_create(name: str) -> None:
    """Create a new (empty) named configuration."""
    invocation().store.create_profile(name)
    typer.echo(f"Created configuration [{name}].")


@configurations_app.command("delete")
def configurations_delete(name: str) -> None:
    """Delete a named configuration."""
    invocation().store.delete_profile(name)
    typer.echo(f"Deleted configuration [{name}].")


def attach_builtins(cli: SpecCli) -> None:
    """Mount the hand-written commands onto the generated tree."""
    cli.command("login")(login)
    cli.command("logout")(logout)
    cli.command("auth", "list")(auth_list)
    cli.command("auth", "print-access-token")(print_access_token)
    cli.add_typer(config_app, "config")
