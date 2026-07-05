#  Project:      dfe-engine
#  File:         cli/governed_ops.py
#  Purpose:      CLI for Governed Ops (thin wrapper over the same services the API uses)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""`dfe-api governed ...` - helm-var CRUD + defined actions over the gitops repo.

A thin wrapper: it calls the SAME GitCrud / ActionStore the API routers call, so
every write takes the identical gitops-commit path (the standing "CLI is a wrapper
over the API" rule). Useful for CI / break-glass when the API is unavailable.
"""

from __future__ import annotations

import json

import typer

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.commit_policy import CommitPolicyError, validate_change
from dfe_engine.gitcrud.factory import build_gitcrud
from dfe_engine.governance import (
    ActionForbiddenError,
    ActionStore,
    PolicyStore,
    ProtectedVarError,
)
from dfe_engine.settings import load_settings

governed_app = typer.Typer(help="Governed Ops: helm vars + defined actions over gitops.")
helm_app = typer.Typer(help="Tier-1 helm-var CRUD.")
action_app = typer.Typer(help="Tier-2 defined actions.")
governed_app.add_typer(helm_app, name="helm")
governed_app.add_typer(action_app, name="action")


def _crud() -> GitCrud:
    gc = build_gitcrud(load_settings().gitops)
    if gc is None:
        typer.echo("gitops is not enabled (set DFE_GITOPS_ENABLED + DFE_GITOPS_*)", err=True)
        raise typer.Exit(1)
    return gc


def _parse(value: str) -> object:
    """Parse a CLI value as JSON (numbers/bools/objects), else keep the raw string."""
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


@helm_app.command("list")
def helm_list() -> None:
    """List helm-var overlay resources."""
    for name in _crud().list("helmvars"):
        typer.echo(name)


@helm_app.command("get")
def helm_get(name: str) -> None:
    """Show a resource's flattened dot-path vars."""
    for path, value in _crud().vars("helmvars", name).items():
        typer.echo(f"{path} = {value!r}")


@helm_app.command("set")
def helm_set(name: str, path: str, value: str, actor: str = "cli", override: bool = False) -> None:
    """Set a helm var (-> gitops commit). VALUE is parsed as JSON when possible.

    Applies the SAME guards as the /helm API (the module contract): the
    immutability validators (no floating image / controller-owned replicaCount)
    and the protected-var policy. --override is the break-glass helmvars:override
    equivalent. A refused write exits non-zero (CI/scripts must see the failure).
    """
    gc = _crud()
    val = _parse(value)
    try:
        validate_change(path, val)
    except CommitPolicyError as exc:
        typer.echo(f"policy violation: {exc}", err=True)
        raise typer.Exit(1) from exc
    try:
        PolicyStore(gc).enforce("helmvars", name, path, override=override)
    except ProtectedVarError as exc:
        typer.echo(f"protected var (use --override to force): {exc}", err=True)
        raise typer.Exit(1) from exc
    res = gc.set_key("helmvars", name, path, val, actor)
    typer.echo(res.commit_sha or "(unchanged)")


@action_app.command("list")
def action_list() -> None:
    """List defined actions."""
    for name in ActionStore(_crud()).list():
        typer.echo(name)


@action_app.command("invoke")
def action_invoke(
    name: str, actor: str = "cli", dry_run: bool = False, override: bool = False
) -> None:
    """Invoke a defined action (use --dry-run to preview the diff).

    Enforces the protected-var policy like the API (POST .../invoke); --override
    is the break-glass equivalent. A refused invoke exits non-zero.
    """
    gc = _crud()
    try:
        res = ActionStore(gc).invoke(
            name, actor, policy=PolicyStore(gc), dry_run=dry_run, override=override
        )
    except (ProtectedVarError, ActionForbiddenError, CommitPolicyError) as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(1) from exc
    for d in res.diff:
        typer.echo(f"{d['cls']}/{d['name']}:{d['path']}  {d['old']!r} -> {d['new']!r}")
    typer.echo(f"dry_run={res.dry_run} changed={res.changed} commit={res.commit_sha or '-'}")


@governed_app.command("reconcile-ch-rbac")
def reconcile_ch_rbac_cmd() -> None:
    """Reconcile CH quota tiers + service roles + fixed users + the per-table
    DFE_current_tenant_id row policies into ClickHouse (mints the service + fixed
    user secrets via the secrets seam). Idempotent.
    """
    from pathlib import Path

    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.governance.ch import reconcile_ch_rbac
    from dfe_engine.orgs.registry import OrgRegistry
    from dfe_engine.secrets import build_secrets

    settings = load_settings()
    ch_cfg = {
        "ch_host": settings.clickhouse.host,
        "ch_port": settings.clickhouse.port,
        "ch_username": settings.clickhouse.username,
        "ch_password": settings.clickhouse.password,
        "ch_secure": settings.clickhouse.secure,
        "ch_verify": settings.clickhouse.verify,
    }
    admin_client = ClickHouseManager.get_instance(ch_cfg).get_clickhouse_client()._client
    orgs_dir = Path(settings.config_dir or "config") / "orgs"
    orgs = OrgRegistry(orgs_dir).list() if orgs_dir.exists() else []
    result = reconcile_ch_rbac(
        admin_client,
        secrets_store=build_secrets(settings.secrets),
        orgs=orgs,
    )
    typer.echo(
        f"applied={len(result.statements)} dropped={len(result.dropped)} "
        f"minted={len(result.minted)} errors={len(result.errors)}"
    )
    for err in result.errors:
        typer.echo(f"  error: {err}", err=True)
    # Partial reconcile is a failure the caller (CI/scripts) must see - exit
    # non-zero when any statement errored, don't mask it behind a 0 exit.
    if result.errors:
        raise typer.Exit(1)


def register_governed_ops_commands(app: typer.Typer) -> None:
    """Register the ``governed`` subcommand group on *app*."""
    app.add_typer(governed_app, name="governed")
