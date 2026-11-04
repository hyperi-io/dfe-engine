#  Project:      dfe-engine
#  File:         cli/auto/app.py
#  Purpose:      Assemble the `dfe` CLI: scalo's SpecCli over the engine spec plus the engine-only commands
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Root of the ``dfe`` CLI.

scalo's ``SpecCli`` builds the command tree from the engine's OpenAPI document,
so an endpoint becomes a command with no CLI code. The commands with no
operation behind them are mounted on top: ``login``, ``config``, ``auth list``,
and the daemon-free ``local``, ``schema`` and ``kafka client-config``.
"""

import os
import re
import secrets
from typing import Any

from scalo.cli.openapi import ScaloHttpTransport, SpecCli, Store, Transport

from .authcmds import attach_builtins
from .kafka import client_config
from .local import local_app
from .schema import schema_app


def load_live_spec() -> dict[str, Any]:
    """The engine OpenAPI document, read from the in-process app factory.

    The CLI signs no tokens, but ``DFESettings`` refuses the production posture without a
    jwt_secret, which a schema Job does not carry. A throwaway one satisfies that check.
    """
    from dfe_engine.api.app import create_app

    os.environ.setdefault("DFE_ENGINE_API_JWT_SECRET", secrets.token_urlsafe(32))

    return create_app().openapi()


def _positional_path_params(spec: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``spec`` taking every path parameter as an argument, in path order, unless the operation names its own."""
    paths = {}
    for path, item in spec["paths"].items():
        names = re.findall(r"{([^}]+)}", path)
        paths[path] = {
            method: _with_positional(op, names)
            if names and isinstance(op, dict) and "x-scalo" in op
            else op
            for method, op in item.items()
        }
    return {**spec, "paths": paths}


def _with_positional(op: dict[str, Any], names: list[str]) -> dict[str, Any]:
    cli = op["x-scalo"].get("cli") or {}
    if cli.get("positional"):
        return op
    return {**op, "x-scalo": {**op["x-scalo"], "cli": {**cli, "positional": names}}}


def build_cli(
    spec: dict[str, Any] | None = None,
    *,
    transport: Transport | None = None,
    store: Store | None = None,
) -> SpecCli:
    """Build the ``dfe`` CLI.

    Args:
        spec: The OpenAPI document, the live engine spec by default.
        transport: Sends the requests, ``scalo.http`` by default.
        store: Profiles and credentials, ``~/.config/dfe`` by default.

    Returns:
        The CLI, ready to ``main()``.
    """
    cli = SpecCli(
        _positional_path_params(spec if spec is not None else load_live_spec()),
        name="dfe",
        env_prefix="DFE_ENGINE",
        help="dfe - the Data Fusion Engine CLI. Command tree generated from the engine OpenAPI spec.",
        transport=transport or ScaloHttpTransport(),
        store=store,
    )
    attach_builtins(cli)
    cli.add_typer(local_app, "local")
    cli.add_typer(schema_app, "schema")
    cli.command("kafka", "client-config")(client_config)
    return cli
