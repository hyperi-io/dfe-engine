#  Project:      dfe-engine
#  File:         cli/auto/authcmds.py
#  Purpose:      Hand-written login/logout/auth/config built-in command groups
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Built-in (non-generated) command groups: ``login``/``logout``/``auth``/``config``.

These are the credential + configuration surface that has no OpenAPI operation of
its own (login/refresh are ``x-cli`` hidden precisely because the CLI handles them
here - it stores the returned JWT in the credential file). Everything else in the
tree is generated from the spec.
"""

from __future__ import annotations

import click
import httpx
import httpx2

from .build import HelpGroup
from .config import Credential, Store
from .errors import HTTP_ERRORS, DfeConfigError, handle


def _store(ctx: click.Context) -> Store:
    return ctx.obj.store


def _handle_http(ctx: click.Context, exc: httpx.HTTPError | httpx2.HTTPError) -> None:
    """Render an HTTP failure from a built-in the same way generated commands do.

    The hand-written built-ins bypass the generated ``_make_callback`` wrapper, so
    without this a bad-credentials login re-raises a raw ``HTTPStatusError``
    (traceback, exit 1). Route it through ``errors.handle`` for the friendly
    ``Error (401): ...`` line + the right taxonomy exit code.
    """
    opts = ctx.obj
    emit = opts.emit if opts.out_stream is not None else None
    code = handle(exc, debug=opts.debug, emit=emit)
    ctx.exit(code)


def _emit(ctx: click.Context, message: str) -> None:
    """Write built-in command output through the (injectable) sink.

    Routes via ``GlobalOptions.emit`` so output is captured deterministically in
    tests and lands on the terminal in normal use - the same path the generated
    commands take.
    """
    ctx.obj.emit(message)


# -- login / logout ----------------------------------------------------------


@click.command()
@click.option("--url", help="Engine base URL, e.g. https://engine.example:8000")
@click.option("--api-key", help="Authenticate with a static API key.")
@click.option("--username", "-u", help="Username for password login.")
@click.option("--password", "-p", help="Password for password login.")
@click.option("--configuration", help="Named configuration (profile) to write to.")
@click.pass_context
def login(
    ctx: click.Context,
    url: str | None,
    api_key: str | None,
    username: str | None,
    password: str | None,
    configuration: str | None,
) -> None:
    """Authenticate and store credentials for later commands.

    Either pass --api-key, or --username/--password to exchange for a JWT. With no
    new credentials but a valid stored one, just re-activates the profile.
    """
    store = _store(ctx)
    profile = configuration or store.active_profile()

    if url:
        store.set_value("url", url, profile)
    effective_url = url or store.get_value("url", profile)
    if not effective_url:
        raise DfeConfigError("--url is required for the first login to a profile.")

    account: str | None = None
    credential: Credential | None = None

    try:
        if api_key:
            # Resolve the account label from /auth/me when possible; fall back to a
            # generic label so the credential is still keyable.
            account = _whoami(ctx, effective_url, Credential(account="?", api_key=api_key))
            account = account or username or "api-key"
            credential = Credential(account=account, api_key=api_key)
        elif username and password:
            token = _password_login(ctx, effective_url, username, password)
            account = username
            credential = Credential(account=account, token=token)
        else:
            # No new creds: re-activate an existing one (gcloud shortcut).
            account = store.get_value("account", profile)
            if not account or store.get_credential(account) is None:
                raise DfeConfigError(
                    "No stored credentials for this profile. Pass --api-key or "
                    "--username/--password."
                )
    except HTTP_ERRORS as exc:
        # e.g. bad password -> 401. Render friendly + right exit code (not a raw
        # traceback), the same way the generated commands do.
        _handle_http(ctx, exc)

    if credential is not None:
        store.save_credential(credential)
        store.set_value("account", account, profile)
    store.set_active_profile(profile)

    _emit(ctx, f"You are now logged in as {account}. Active endpoint: {effective_url}")


@click.command()
@click.option("--configuration", help="Named configuration (profile) to clear.")
@click.pass_context
def logout(ctx: click.Context, configuration: str | None) -> None:
    """Clear the stored credential + active account for a profile."""
    store = _store(ctx)
    profile = configuration or store.active_profile()
    account = store.get_value("account", profile)
    if account:
        store.delete_credential(account)
        store.set_value("account", "", profile)
        _emit(ctx, f"Logged out {account}.")
    else:
        _emit(ctx, "No active account to log out.")
    ctx.invoke(auth_list)


def _password_login(ctx: click.Context, url: str, username: str, password: str) -> str:
    """POST /api/v1/auth/login and return the access token."""
    # retries=0: the login POST is non-idempotent - never auto-retry it.
    client = ctx.obj.client_for(url, None, retries=0)
    try:
        response = client.raw(
            "POST",
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        return response.json()["access_token"]
    finally:
        client.close()


def _whoami(ctx: click.Context, url: str, credential: Credential) -> str | None:
    """GET /api/v1/auth/me and return the user_id, or None on any failure."""
    client = ctx.obj.client_for(url, credential)
    try:
        headers = (
            {"X-API-Key": credential.api_key}
            if credential.is_api_key
            else {"Authorization": f"Bearer {credential.token}"}
        )
        response = client.raw("GET", "/api/v1/auth/me", headers=headers)
        return response.json().get("user_id")
    except Exception:
        return None
    finally:
        client.close()


# -- auth group --------------------------------------------------------------


@click.group("auth", cls=HelpGroup, no_args_is_help=True)
def auth_group() -> None:
    """Inspect stored credentials + profiles."""


@auth_group.command("list")
@click.pass_context
def auth_list(ctx: click.Context) -> None:
    """List configured accounts, marking the active one."""
    store = _store(ctx)
    active_profile = store.active_profile()
    active_account = store.get_value("account", active_profile)
    accounts = store.list_accounts()
    if not accounts:
        _emit(ctx, "No credentials stored. Run `dfe login ...`.")
        return
    for account in accounts:
        marker = " (active)" if account == active_account else ""
        _emit(ctx, f"{account}{marker}")


@auth_group.command("print-access-token")
@click.pass_context
def auth_print_token(ctx: click.Context) -> None:
    """Print the active token to stdout (for scripts)."""
    store = _store(ctx)
    profile = ctx.obj.configuration or store.active_profile()
    account = store.get_value("account", profile)
    if not account:
        raise DfeConfigError("No active account.")
    credential = store.get_credential(account)
    if credential is None or credential.value is None:
        raise DfeConfigError("No stored token for the active account.")
    _emit(ctx, credential.value or "")


# -- config group ------------------------------------------------------------


@click.group("config", cls=HelpGroup, no_args_is_help=True)
def config_group() -> None:
    """Read and write CLI configuration (INI-backed profiles)."""


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """Set a config KEY to VALUE in the active profile."""
    _store(ctx).set_value(key, value)
    _emit(ctx, f"Set {key}.")


@config_group.command("get")
@click.argument("key")
@click.pass_context
def config_get(ctx: click.Context, key: str) -> None:
    """Print a single config KEY from the active profile."""
    value = _store(ctx).get_value(key)
    if value is None:
        raise DfeConfigError(f"'{key}' is not set.")
    _emit(ctx, value)


@config_group.command("list")
@click.pass_context
def config_list(ctx: click.Context) -> None:
    """Print all config keys for the active profile."""
    store = _store(ctx)
    items = store.profile_items()
    _emit(ctx, f"[{store.active_profile()}]")
    for key, value in items.items():
        _emit(ctx, f"{key} = {value}")


@config_group.group("configurations", cls=HelpGroup, no_args_is_help=True)
def configurations_group() -> None:
    """Manage named configurations (profiles)."""


@configurations_group.command("list")
@click.pass_context
def configurations_list(ctx: click.Context) -> None:
    """List named configurations, marking the active one."""
    store = _store(ctx)
    active = store.active_profile()
    names = store.list_profiles() or [active]
    for name in names:
        marker = " (active)" if name == active else ""
        _emit(ctx, f"{name}{marker}")


@configurations_group.command("activate")
@click.argument("name")
@click.pass_context
def configurations_activate(ctx: click.Context, name: str) -> None:
    """Make NAME the active configuration."""
    _store(ctx).set_active_profile(name)
    _emit(ctx, f"Activated configuration [{name}].")


@configurations_group.command("create")
@click.argument("name")
@click.pass_context
def configurations_create(ctx: click.Context, name: str) -> None:
    """Create a new (empty) named configuration."""
    _store(ctx).create_profile(name)
    _emit(ctx, f"Created configuration [{name}].")


@configurations_group.command("delete")
@click.argument("name")
@click.pass_context
def configurations_delete(ctx: click.Context, name: str) -> None:
    """Delete a named configuration."""
    _store(ctx).delete_profile(name)
    _emit(ctx, f"Deleted configuration [{name}].")


def _merge_group(root: click.Group, builtin: click.Group) -> None:
    """Merge a built-in group's commands into any same-named generated group.

    The spec already produces an ``auth`` group (accounts/groups/roles/...); the
    built-in ``auth list``/``print-access-token`` must live ALONGSIDE those, not
    replace them. If no generated group exists (e.g. ``config`` has no exposed API
    operations) the built-in group is mounted whole.
    """
    existing = root.commands.get(builtin.name)
    if isinstance(existing, click.Group):
        for name, command in builtin.commands.items():
            existing.add_command(command, name)
    else:
        root.add_command(builtin)


def attach_builtins(root: click.Group) -> None:
    """Mount the hand-written groups onto the root (merging where names collide)."""
    root.add_command(login)
    root.add_command(logout)
    _merge_group(root, auth_group)
    _merge_group(root, config_group)
