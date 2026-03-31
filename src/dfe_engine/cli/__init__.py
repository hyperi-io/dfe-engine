#  Project:      dfe-engine
#  File:         cli/__init__.py
#  Purpose:      CLI subcommands for auth management (accounts, groups, API keys)
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Auth management CLI subcommands for ``dfe-api``.

Registered via :meth:`DfeApiApp.register_commands` and exposed as::

    dfe-api accounts <subcommand>
    dfe-api groups <subcommand>
    dfe-api api-keys <subcommand>
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated

import typer
from tabulate import tabulate

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.bootstrap import bootstrap_auth
from dfe_engine.auth.groups import GroupStore


def _get_stores() -> tuple[AccountStore, GroupStore, APIKeyStore]:
    """Initialise auth stores from the configured directory.

    Reads ``DFE_AUTH_DIR`` (preferred) or derives it from ``DFE_CONFIG_DIR``
    (defaults to ``./config``).  Always returns working stores — empty
    directories are fine.
    """
    config_dir = os.environ.get("DFE_CONFIG_DIR", "./config")
    auth_dir = os.environ.get("DFE_AUTH_DIR", str(Path(config_dir) / "auth"))
    admin_pw = os.environ.get("DFE_ADMIN_PASSWORD", "changeme")
    account_store, group_store, api_key_store, _ = bootstrap_auth(
        Path(auth_dir), default_admin_password=admin_pw
    )
    return account_store, group_store, api_key_store


# ---------------------------------------------------------------------------
# accounts subcommands
# ---------------------------------------------------------------------------

accounts_app = typer.Typer(help="Manage local accounts.")


@accounts_app.command("create")
def accounts_create(
    username: str,
    groups: Annotated[str, typer.Option(help="Comma-separated group names.")] = "",
    generate_password: Annotated[
        bool, typer.Option("--generate-password", help="Generate a random password.")
    ] = False,
) -> None:
    """Create a new local account."""
    account_store, group_store, _ = _get_stores()

    if generate_password:
        password = secrets.token_urlsafe(16)
    else:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)

    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    try:
        account_store.create(username, password, groups=group_list)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    # Add the user to each group's member list as well
    for group_name in group_list:
        try:
            group_store.add_member(group_name, username)
        except KeyError:
            typer.echo(f"Warning: group '{group_name}' not found — skipped", err=True)

    if generate_password:
        typer.echo(f"Account '{username}' created.")
        typer.echo(f"Generated password: {password}")
        typer.echo("Store this password securely — it cannot be retrieved again.")
    else:
        typer.echo(f"Account '{username}' created.")


@accounts_app.command("list")
def accounts_list() -> None:
    """List all local accounts."""
    account_store, _, _ = _get_stores()
    accounts = account_store.list()
    if not accounts:
        typer.echo("No accounts found.")
        return

    rows = [
        [
            a.username,
            "yes" if a.enabled else "no",
            ", ".join(a.groups) or "—",
            a.created_at[:19] if a.created_at else "—",
        ]
        for a in accounts
    ]
    typer.echo(
        tabulate(rows, headers=["Username", "Enabled", "Groups", "Created At"], tablefmt="plain")
    )


@accounts_app.command("show")
def accounts_show(username: str) -> None:
    """Show details for an account."""
    account_store, _, _ = _get_stores()
    account = account_store.get(username)
    if account is None:
        typer.echo(f"Error: account '{username}' not found.", err=True)
        raise typer.Exit(1)

    rows = [
        ["Username", account.username],
        ["Enabled", "yes" if account.enabled else "no"],
        ["Groups", ", ".join(account.groups) or "—"],
        ["Created At", account.created_at or "—"],
        ["Updated At", account.updated_at or "—"],
    ]
    typer.echo(tabulate(rows, tablefmt="plain"))


@accounts_app.command("enable")
def accounts_enable(username: str) -> None:
    """Enable a local account."""
    account_store, _, _ = _get_stores()
    try:
        account_store.update(username, enabled=True)
    except KeyError:
        typer.echo(f"Error: account '{username}' not found.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Account '{username}' enabled.")


@accounts_app.command("disable")
def accounts_disable(username: str) -> None:
    """Disable a local account."""
    account_store, _, _ = _get_stores()
    try:
        account_store.update(username, enabled=False)
    except KeyError:
        typer.echo(f"Error: account '{username}' not found.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Account '{username}' disabled.")


@accounts_app.command("reset-password")
def accounts_reset_password(username: str) -> None:
    """Reset the password for a local account."""
    account_store, _, _ = _get_stores()
    new_password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    try:
        account_store.reset_password(username, new_password)
    except KeyError:
        typer.echo(f"Error: account '{username}' not found.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Password reset for account '{username}'.")


@accounts_app.command("delete")
def accounts_delete(
    username: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Delete a local account."""
    if not yes:
        typer.confirm(f"Delete account '{username}'?", abort=True)

    account_store, _, _ = _get_stores()
    try:
        account_store.delete(username)
    except KeyError:
        typer.echo(f"Error: account '{username}' not found.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Account '{username}' deleted.")


# ---------------------------------------------------------------------------
# groups subcommands
# ---------------------------------------------------------------------------

groups_app = typer.Typer(help="Manage groups.")


@groups_app.command("create")
def groups_create(
    name: str,
    roles: Annotated[str, typer.Option("--roles", help="Comma-separated role names.")],
    description: Annotated[str, typer.Option("--description", help="Group description.")] = "",
) -> None:
    """Create a new group."""
    _, group_store, _ = _get_stores()
    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    try:
        group_store.create(name, roles=role_list, description=description)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Group '{name}' created.")


@groups_app.command("list")
def groups_list() -> None:
    """List all groups."""
    _, group_store, _ = _get_stores()
    groups = group_store.list()
    if not groups:
        typer.echo("No groups found.")
        return

    rows = [
        [
            g.name,
            ", ".join(g.roles) or "—",
            ", ".join(g.members) or "—",
            g.description or "—",
        ]
        for g in groups
    ]
    typer.echo(
        tabulate(rows, headers=["Name", "Roles", "Members", "Description"], tablefmt="plain")
    )


@groups_app.command("show")
def groups_show(name: str) -> None:
    """Show details for a group."""
    _, group_store, _ = _get_stores()
    group = group_store.get(name)
    if group is None:
        typer.echo(f"Error: group '{name}' not found.", err=True)
        raise typer.Exit(1)

    rows = [
        ["Name", group.name],
        ["Description", group.description or "—"],
        ["Roles", ", ".join(group.roles) or "—"],
        ["Members", ", ".join(group.members) or "—"],
    ]
    typer.echo(tabulate(rows, tablefmt="plain"))


@groups_app.command("add-member")
def groups_add_member(group: str, username: str) -> None:
    """Add a user to a group."""
    _, group_store, _ = _get_stores()
    try:
        group_store.add_member(group, username)
    except KeyError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Added '{username}' to group '{group}'.")


@groups_app.command("remove-member")
def groups_remove_member(group: str, username: str) -> None:
    """Remove a user from a group."""
    _, group_store, _ = _get_stores()
    try:
        group_store.remove_member(group, username)
    except KeyError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Removed '{username}' from group '{group}'.")


@groups_app.command("set-roles")
def groups_set_roles(
    name: str,
    roles: Annotated[str, typer.Argument(help="Comma-separated role names.")],
) -> None:
    """Replace the role list for a group."""
    _, group_store, _ = _get_stores()
    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    try:
        group_store.update(name, roles=role_list)
    except KeyError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Roles for group '{name}' updated.")


@groups_app.command("delete")
def groups_delete(
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Delete a group."""
    if not yes:
        typer.confirm(f"Delete group '{name}'?", abort=True)

    _, group_store, _ = _get_stores()
    try:
        group_store.delete(name)
    except KeyError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Group '{name}' deleted.")


# ---------------------------------------------------------------------------
# api-keys subcommands
# ---------------------------------------------------------------------------

api_keys_app = typer.Typer(help="Manage API keys.")


@api_keys_app.command("create")
def api_keys_create(
    name: str,
    groups: Annotated[str, typer.Option(help="Comma-separated group names.")] = "",
    description: Annotated[str, typer.Option("--description", help="Key description.")] = "",
) -> None:
    """Create a new API key."""
    _, _, api_key_store = _get_stores()
    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    try:
        _key_meta, full_key = api_key_store.create(name, groups=group_list, description=description)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"API Key: {full_key}")
    typer.echo("Store this key securely — it cannot be retrieved again.")


@api_keys_app.command("list")
def api_keys_list() -> None:
    """List all API keys."""
    _, _, api_key_store = _get_stores()
    keys = api_key_store.list()
    if not keys:
        typer.echo("No API keys found.")
        return

    rows = [
        [
            k.name,
            k.short_token,
            "yes" if k.enabled else "no",
            ", ".join(k.groups) or "—",
            k.created_at[:19] if k.created_at else "—",
        ]
        for k in keys
    ]
    typer.echo(
        tabulate(
            rows,
            headers=["Name", "Short Token", "Enabled", "Groups", "Created At"],
            tablefmt="plain",
        )
    )


@api_keys_app.command("revoke")
def api_keys_revoke(
    short_token: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Revoke an API key by its short token."""
    if not yes:
        typer.confirm(f"Revoke API key with short token '{short_token}'?", abort=True)

    _, _, api_key_store = _get_stores()
    try:
        api_key_store.revoke(short_token)
    except KeyError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"API key '{short_token}' revoked.")


def register_auth_commands(app: typer.Typer) -> None:
    """Register auth subcommand groups on *app*.

    Call this from :meth:`DfeApiApp.register_commands`.
    """
    app.add_typer(accounts_app, name="accounts")
    app.add_typer(groups_app, name="groups")
    app.add_typer(api_keys_app, name="api-keys")
