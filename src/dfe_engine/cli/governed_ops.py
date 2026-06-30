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
from dfe_engine.gitcrud.factory import build_gitcrud
from dfe_engine.governance import ActionStore
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
def helm_set(name: str, path: str, value: str, actor: str = "cli") -> None:
    """Set a helm var (-> gitops commit). VALUE is parsed as JSON when possible."""
    res = _crud().set_key("helmvars", name, path, _parse(value), actor)
    typer.echo(res.commit_sha or "(unchanged)")


@action_app.command("list")
def action_list() -> None:
    """List defined actions."""
    for name in ActionStore(_crud()).list():
        typer.echo(name)


@action_app.command("invoke")
def action_invoke(name: str, actor: str = "cli", dry_run: bool = False) -> None:
    """Invoke a defined action (use --dry-run to preview the diff)."""
    res = ActionStore(_crud()).invoke(name, actor, dry_run=dry_run)
    for d in res.diff:
        typer.echo(f"{d['cls']}/{d['name']}:{d['path']}  {d['old']!r} -> {d['new']!r}")
    typer.echo(f"dry_run={res.dry_run} changed={res.changed} commit={res.commit_sha or '-'}")


def register_governed_ops_commands(app: typer.Typer) -> None:
    """Register the ``governed`` subcommand group on *app*."""
    app.add_typer(governed_app, name="governed")
