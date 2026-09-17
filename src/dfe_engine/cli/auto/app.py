#  Project:      dfe-engine
#  File:         cli/auto/app.py
#  Purpose:      Assemble the root `dfe` click group (generated + built-ins)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Root of the ``dfe`` CLI: global flags, the generated tree and the built-ins.

Global flags live on the root group and are propagated to every command via the
click context object (a ``GlobalOptions``). The generated command tree is built
from the live OpenAPI spec; the hand-written ``login``/``logout``/``auth``/``config``
groups are mounted alongside. An unknown subcommand triggers a difflib
"did you mean 'X'?" hint.
"""

from __future__ import annotations

import difflib
from typing import Any

import click

from .authcmds import attach_builtins
from .build import GlobalOptions, HelpGroup, build_command_tree
from .config import Store
from .kafka import attach_kafka
from .local import attach_local
from .schema import attach_schema
from .spec import iter_operations, load_live_spec

_FORMATS = ("json", "yaml", "table", "value", "text")


class DfeGroup(HelpGroup):
    """Root group: help-on-no-args (exit 0) + a did-you-mean hint on typos."""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            cmd_name = args[0] if args else ""
            matches = difflib.get_close_matches(
                cmd_name, list(self.list_commands(ctx)), n=1, cutoff=0.6
            )
            hint = f" Did you mean '{matches[0]}'?" if matches else ""
            raise click.UsageError(f"No such command '{cmd_name}'.{hint}", ctx)


def _root_callback(
    ctx: click.Context,
    format: str | None,
    query: str | None,
    quiet: bool,
    url: str | None,
    configuration: str | None,
    debug: bool,
    no_paginate: bool,
) -> None:
    # Merge flags into an injected GlobalOptions (tests hand one in via `obj=`)
    # or a fresh one. Never clobber an injected store / client_factory.
    opts = ctx.obj if isinstance(ctx.obj, GlobalOptions) else GlobalOptions()
    opts.format = format or opts.format
    opts.query = query or opts.query
    opts.quiet = quiet or opts.quiet
    opts.url = url or opts.url
    opts.configuration = configuration or opts.configuration
    opts.debug = debug or opts.debug
    opts.no_paginate = no_paginate or opts.no_paginate
    ctx.obj = opts


def build_root(
    spec: dict[str, Any] | None = None,
    *,
    obj: GlobalOptions | None = None,
) -> DfeGroup:
    """Build and return the fully assembled root command group.

    ``spec`` defaults to the live engine spec; tests may pass a spec built from a
    test app to avoid re-loading and to match their settings. ``obj`` seeds the
    context (tests inject a store + client_factory here).
    """
    root = DfeGroup(
        name="dfe",
        no_args_is_help=True,
        help=(
            "dfe - the Data Fusion Engine CLI. Command tree generated from the "
            "engine OpenAPI spec; speaks HTTP to a running engine."
        ),
        params=[
            click.Option(
                ["--format", "format"],
                type=click.Choice(_FORMATS),
                help="Output format (default: yaml for describe, table for list, "
                "else json; json when not a tty).",
            ),
            click.Option(["--query"], help="JMESPath filter applied before output."),
            click.Option(
                ["--quiet", "-q", "quiet"],
                is_flag=True,
                help="Auto-confirm prompts and suppress interactive output.",
            ),
            click.Option(["--url"], help="Engine base URL (overrides the profile)."),
            click.Option(["--configuration"], help="Named configuration (profile) to use."),
            click.Option(["--debug"], is_flag=True, help="Print tracebacks on error."),
            click.Option(
                ["--no-paginate", "no_paginate"],
                is_flag=True,
                help="Fetch only a single page for list commands.",
            ),
        ],
        callback=click.pass_context(_root_callback),
    )

    spec = spec if spec is not None else load_live_spec()
    build_command_tree(root, iter_operations(spec))
    attach_builtins(root)
    # `dfe local` - break-glass direct gitops CRUD for when the daemon is dead.
    # Mounted alongside the generated tree; it speaks to the local clone, not HTTP.
    attach_local(root)
    # `dfe kafka` - emit client config for a provider (kcat / confluent / librdkafka),
    # so a CLI "just works" against the DFE brokers. Local; no HTTP to the engine.
    attach_kafka(root)
    # `dfe schema` - plan, apply and read the schema against the local ClickHouse.
    # Takes the same lease the boot phase does, so it serialises rather than races.
    attach_schema(root)

    if obj is not None:
        # Stash the injected object as the group's default context object.
        root.context_settings = {"obj": obj}

    return root


def make_root() -> DfeGroup:
    """Entry-point helper: build the root with a default (live) config store."""
    return build_root(obj=GlobalOptions(store=Store()))
