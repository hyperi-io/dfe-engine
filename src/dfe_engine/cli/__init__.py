#  Project:      dfe-engine
#  File:         cli/__init__.py
#  Purpose:      CLI subcommands for auth management (accounts, groups, API keys)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Auth management CLI subcommands for ``dfe-api``.

Uses hyperi-pylib CLI framework (Typer + Rich output helpers).

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
from scalo.cli import Typer
from scalo.cli.output import (
    print_error,
    print_info,
    print_success,
    print_table,
    print_warning,
)

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.bootstrap import bootstrap_auth
from dfe_engine.auth.groups import GroupStore


def _get_stores() -> tuple[AccountStore, GroupStore, APIKeyStore]:
    """Initialise auth stores from the configured directory."""
    config_dir = os.environ.get("DFE_CONFIG_DIR", "./config")
    auth_dir = os.environ.get("DFE_AUTH_DIR", str(Path(config_dir) / "auth"))
    admin_pw = os.environ.get("DFE_ADMIN_PASSWORD", "changeme")
    account_store, group_store, api_key_store, _, _ = bootstrap_auth(
        Path(auth_dir), default_admin_password=admin_pw
    )
    return account_store, group_store, api_key_store


# ---------------------------------------------------------------------------
# accounts subcommands
# ---------------------------------------------------------------------------

accounts_app = Typer(help="Manage local accounts.")


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
        print_error(str(exc))
        raise typer.Exit(1) from exc

    from dfe_engine.auth.membership import sync_group_members_for_account_groups_change

    sync_group_members_for_account_groups_change(
        group_store,
        username,
        added=group_list,
    )

    if generate_password:
        print_success(f"Account '{username}' created")
        print_info(f"Generated password: {password}")
        print_warning("Store this password securely — it cannot be retrieved again")
    else:
        print_success(f"Account '{username}' created")


@accounts_app.command("list")
def accounts_list() -> None:
    """List all local accounts."""
    account_store, _, _ = _get_stores()
    accounts = account_store.list()
    if not accounts:
        print_info("No accounts found")
        return

    data = [
        {
            "Username": a.username,
            "Enabled": "yes" if a.enabled else "no",
            "Groups": ", ".join(a.groups) or "-",
            "Created": a.created_at[:19] if a.created_at else "-",
        }
        for a in accounts
    ]
    print_table(data, title="Accounts")


@accounts_app.command("show")
def accounts_show(username: str) -> None:
    """Show details for an account."""
    account_store, _, _ = _get_stores()
    account = account_store.get(username)
    if account is None:
        print_error(f"Account '{username}' not found")
        raise typer.Exit(1)

    data = [
        {"Field": "Username", "Value": account.username},
        {"Field": "Enabled", "Value": "yes" if account.enabled else "no"},
        {"Field": "Groups", "Value": ", ".join(account.groups) or "-"},
        {"Field": "Created", "Value": account.created_at or "-"},
        {"Field": "Updated", "Value": account.updated_at or "-"},
    ]
    print_table(data, title=f"Account: {username}")


@accounts_app.command("enable")
def accounts_enable(username: str) -> None:
    """Enable a local account."""
    account_store, _, _ = _get_stores()
    try:
        account_store.update(username, enabled=True)
    except KeyError:
        print_error(f"Account '{username}' not found")
        raise typer.Exit(1)
    print_success(f"Account '{username}' enabled")


@accounts_app.command("disable")
def accounts_disable(username: str) -> None:
    """Disable a local account."""
    account_store, _, _ = _get_stores()
    try:
        account_store.update(username, enabled=False)
    except KeyError:
        print_error(f"Account '{username}' not found")
        raise typer.Exit(1)
    print_success(f"Account '{username}' disabled")


@accounts_app.command("reset-password")
def accounts_reset_password(username: str) -> None:
    """Reset the password for a local account."""
    account_store, _, _ = _get_stores()
    new_password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    try:
        account_store.reset_password(username, new_password)
    except KeyError:
        print_error(f"Account '{username}' not found")
        raise typer.Exit(1)
    print_success(f"Password reset for account '{username}'")


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
        print_error(f"Account '{username}' not found")
        raise typer.Exit(1)
    print_success(f"Account '{username}' deleted")


# ---------------------------------------------------------------------------
# groups subcommands
# ---------------------------------------------------------------------------

groups_app = Typer(help="Manage groups.")


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
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_success(f"Group '{name}' created")


@groups_app.command("list")
def groups_list() -> None:
    """List all groups."""
    _, group_store, _ = _get_stores()
    groups = group_store.list()
    if not groups:
        print_info("No groups found")
        return

    data = [
        {
            "Name": g.name,
            "Roles": ", ".join(g.roles) or "-",
            "Members": ", ".join(g.members) or "-",
            "Description": g.description or "-",
        }
        for g in groups
    ]
    print_table(data, title="Groups")


@groups_app.command("show")
def groups_show(name: str) -> None:
    """Show details for a group."""
    _, group_store, _ = _get_stores()
    group = group_store.get(name)
    if group is None:
        print_error(f"Group '{name}' not found")
        raise typer.Exit(1)

    data = [
        {"Field": "Name", "Value": group.name},
        {"Field": "Description", "Value": group.description or "-"},
        {"Field": "Roles", "Value": ", ".join(group.roles) or "-"},
        {"Field": "Members", "Value": ", ".join(group.members) or "-"},
    ]
    print_table(data, title=f"Group: {name}")


@groups_app.command("add-member")
def groups_add_member(group: str, username: str) -> None:
    """Add a user to a group."""
    account_store, group_store, _ = _get_stores()
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    try:
        group_store.add_member(group, username)
    except KeyError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    sync_account_groups_for_membership_change(account_store, group, added=[username])
    print_success(f"Added '{username}' to group '{group}'")


@groups_app.command("remove-member")
def groups_remove_member(group: str, username: str) -> None:
    """Remove a user from a group."""
    account_store, group_store, _ = _get_stores()
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    try:
        group_store.remove_member(group, username)
    except KeyError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    sync_account_groups_for_membership_change(account_store, group, removed=[username])
    print_success(f"Removed '{username}' from group '{group}'")


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
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_success(f"Roles for group '{name}' updated")


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
        print_error(str(exc))
        raise typer.Exit(1) from exc
    except ValueError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_success(f"Group '{name}' deleted")


# ---------------------------------------------------------------------------
# api-keys subcommands
# ---------------------------------------------------------------------------

api_keys_app = Typer(help="Manage API keys.")


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
        print_error(str(exc))
        raise typer.Exit(1) from exc

    print_success(f"API key '{name}' created")
    print_info(f"API Key: {full_key}")
    print_warning("Store this key securely — it cannot be retrieved again")


@api_keys_app.command("list")
def api_keys_list() -> None:
    """List all API keys."""
    _, _, api_key_store = _get_stores()
    keys = api_key_store.list()
    if not keys:
        print_info("No API keys found")
        return

    data = [
        {
            "Name": k.name,
            "Short Token": k.short_token,
            "Enabled": "yes" if k.enabled else "no",
            "Groups": ", ".join(k.groups) or "-",
            "Created": k.created_at[:19] if k.created_at else "-",
        }
        for k in keys
    ]
    print_table(data, title="API Keys")


@api_keys_app.command("revoke")
def api_keys_revoke(
    short_token: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Revoke an API key by its short token."""
    if not yes:
        typer.confirm(f"Revoke API key '{short_token}'?", abort=True)

    _, _, api_key_store = _get_stores()
    try:
        api_key_store.revoke(short_token)
    except KeyError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_success(f"API key '{short_token}' revoked")


# ---------------------------------------------------------------------------
# oidc-providers subcommands
# ---------------------------------------------------------------------------

oidc_providers_app = Typer(help="Manage OIDC providers.")


def _get_oidc_registry():
    """Initialise OIDC provider registry from the configured directory."""
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry

    config_dir = os.environ.get("DFE_CONFIG_DIR", "./config")
    auth_dir = os.environ.get("DFE_AUTH_DIR", str(Path(config_dir) / "auth"))
    oidc_dir = Path(auth_dir) / "oidc-providers"
    oidc_dir.mkdir(parents=True, exist_ok=True)
    return OIDCProviderRegistry(oidc_dir)


@oidc_providers_app.command("create")
def oidc_create(
    name: str,
    provider_type: Annotated[
        str, typer.Option("--type", help="Provider type: generic, google, entra_id, okta.")
    ] = "generic",
    display_name: Annotated[str, typer.Option("--display-name", help="Human-readable label.")] = "",
    issuer: Annotated[str, typer.Option("--issuer", help="OIDC issuer URL.")] = "",
    client_id_env: Annotated[
        str, typer.Option("--client-id-env", help="Env var name for OIDC client ID.")
    ] = "",
    mode: Annotated[
        str, typer.Option("--mode", help="Group resolution mode: manual, token_claim, api.")
    ] = "manual",
) -> None:
    """Create a new OIDC provider configuration."""
    from datetime import UTC, datetime

    from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

    registry = _get_oidc_registry()
    provider = OIDCProvider(
        type=provider_type,
        enabled=True,
        display_name=display_name,
        issuer=issuer,
        client_id_env=client_id_env,
        groups=GroupResolutionConfig(mode=mode),
        created_at=datetime.now(UTC).isoformat(),
    )
    try:
        registry.create(name, provider)
    except ValueError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_success(f"OIDC provider '{name}' created")


@oidc_providers_app.command("list")
def oidc_list() -> None:
    """List all OIDC providers."""
    registry = _get_oidc_registry()
    providers = registry.list()
    if not providers:
        print_info("No OIDC providers found")
        return

    data = [
        {
            "Name": name,
            "Type": p.type,
            "Enabled": "yes" if p.enabled else "no",
            "Mode": p.groups.mode,
            "Issuer": p.issuer or "-",
            "Last Sync": p.last_sync_at[:19] if p.last_sync_at else "-",
        }
        for name, p in providers
    ]
    print_table(data, title="OIDC Providers")


@oidc_providers_app.command("show")
def oidc_show(name: str) -> None:
    """Show details for an OIDC provider."""
    registry = _get_oidc_registry()
    provider = registry.get(name)
    if provider is None:
        print_error(f"OIDC provider '{name}' not found")
        raise typer.Exit(1)

    data = [
        {"Field": "Type", "Value": provider.type},
        {"Field": "Enabled", "Value": "yes" if provider.enabled else "no"},
        {"Field": "Display Name", "Value": provider.display_name or "-"},
        {"Field": "Issuer", "Value": provider.issuer or "-"},
        {"Field": "Client ID Env", "Value": provider.client_id_env or "-"},
        {"Field": "Group Mode", "Value": provider.groups.mode},
        {"Field": "Sync Interval", "Value": str(provider.groups.sync_interval)},
        {"Field": "Created", "Value": provider.created_at or "-"},
        {"Field": "Last Sync", "Value": provider.last_sync_at or "-"},
        {"Field": "Sync Status", "Value": provider.last_sync_status or "-"},
    ]
    if provider.sync_error:
        data.append({"Field": "Sync Error", "Value": provider.sync_error})
    print_table(data, title=f"OIDC Provider: {name}")


@oidc_providers_app.command("test")
def oidc_test(name: str) -> None:
    """Test connectivity to an OIDC provider."""
    import asyncio

    from dfe_engine.auth.oidc.adapters import get_adapter

    registry = _get_oidc_registry()
    provider = registry.get(name)
    if provider is None:
        print_error(f"OIDC provider '{name}' not found")
        raise typer.Exit(1)

    adapter = get_adapter(provider)
    success, message = asyncio.get_event_loop().run_until_complete(adapter.test_connection())
    if success:
        print_success(f"Connection test passed: {message}")
    else:
        print_error(f"Connection test failed: {message}")
        raise typer.Exit(1)


@oidc_providers_app.command("sync")
def oidc_sync(name: str) -> None:
    """Force group sync for an OIDC provider."""
    import asyncio

    from dfe_engine.auth.oidc.sync import sync_provider

    registry = _get_oidc_registry()
    if registry.get(name) is None:
        print_error(f"OIDC provider '{name}' not found")
        raise typer.Exit(1)

    _, group_store, _ = _get_stores()
    result = asyncio.get_event_loop().run_until_complete(sync_provider(name, registry, group_store))

    if result.get("error"):
        print_error(f"Sync failed: {result['error']}")
        raise typer.Exit(1)
    if result.get("skipped"):
        print_warning(f"Sync skipped: {result['skipped']}")
        return
    print_success(
        f"Sync complete: {result['created']} created, "
        f"{result['updated']} updated, {result['total']} total"
    )


@oidc_providers_app.command("update")
def oidc_update(
    name: str,
    enabled: Annotated[str | None, typer.Option("--enabled", help="true or false.")] = None,
    mode: Annotated[str | None, typer.Option("--mode", help="Group resolution mode.")] = None,
    sync_interval: Annotated[
        int | None, typer.Option("--sync-interval", help="Seconds between syncs.")
    ] = None,
    display_name: Annotated[
        str | None, typer.Option("--display-name", help="Human-readable label.")
    ] = None,
) -> None:
    """Update an OIDC provider configuration."""
    from dfe_engine.auth.oidc.models import GroupResolutionConfig

    registry = _get_oidc_registry()
    provider = registry.get(name)
    if provider is None:
        print_error(f"OIDC provider '{name}' not found")
        raise typer.Exit(1)

    update_fields: dict[str, object] = {}
    if enabled is not None:
        update_fields["enabled"] = enabled.lower() in ("true", "1", "yes")
    if display_name is not None:
        update_fields["display_name"] = display_name

    # Update groups config if mode or sync_interval changed
    if mode is not None or sync_interval is not None:
        groups_data = provider.groups.model_dump()
        if mode is not None:
            groups_data["mode"] = mode
        if sync_interval is not None:
            groups_data["sync_interval"] = sync_interval
        update_fields["groups"] = GroupResolutionConfig(**groups_data)

    try:
        registry.update(name, **update_fields)
    except KeyError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_success(f"OIDC provider '{name}' updated")


@oidc_providers_app.command("delete")
def oidc_delete(
    name: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Delete an OIDC provider and warn about orphaned groups."""
    registry = _get_oidc_registry()
    if registry.get(name) is None:
        print_error(f"OIDC provider '{name}' not found")
        raise typer.Exit(1)

    # Check for orphaned groups
    _, group_store, _ = _get_stores()
    orphaned = [g for g in group_store.list() if g.source_provider == name]

    if orphaned:
        print_warning(f"The following {len(orphaned)} group(s) will be orphaned:")
        data = [
            {
                "Name": g.name,
                "Roles": ", ".join(g.roles) or "-",
                "Members": str(len(g.members)),
            }
            for g in orphaned
        ]
        print_table(data, title="Orphaned Groups")

    if not yes:
        typer.confirm(f"Delete OIDC provider '{name}'?", abort=True)

    try:
        registry.delete(name)
    except KeyError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_success(f"OIDC provider '{name}' deleted")


def register_auth_commands(app: Typer) -> None:
    """Register auth subcommand groups on *app*.

    Call this from :meth:`DfeApiApp.register_commands`.
    """
    app.add_typer(accounts_app, name="accounts")
    app.add_typer(groups_app, name="groups")
    app.add_typer(api_keys_app, name="api-keys")
    app.add_typer(oidc_providers_app, name="oidc-providers")
