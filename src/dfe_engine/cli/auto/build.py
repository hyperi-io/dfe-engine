#  Project:      dfe-engine
#  File:         cli/auto/build.py
#  Purpose:      Build the click command tree from parsed OpenAPI Operations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Assemble the generated ``dfe`` command tree from ``Operation`` records.

For each operation we mint a ``click.Command`` named after its verb, with a
``click.Argument`` per path param and a ``click.Option`` per query param and body
prop. Commands nest into ``click.Group``s per ``group_path`` segment (every group
is ``no_args_is_help=True``). The callback resolves url + credential, runs the
call (auto-following pagination for ``list``), applies ``--query`` and formats the
result. Nothing here is endpoint-specific - a new API means a new command for free.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import click

from . import output, paginate
from .client import Client
from .config import Resolved, Store, resolve
from .errors import DfeConfigError, handle
from .spec import BodyProp, Operation, Param


class HelpGroup(click.Group):
    """A group that prints help and exits 0 when invoked with no subcommand.

    click 8.x raises ``NoArgsIsHelpError`` (a UsageError -> exit 2) for a bare
    group; we want ``dfe`` / ``dfe orgs`` to show help as a success (exit 0), the
    aws-cli/gcloud convention.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if not args and self.no_args_is_help and not ctx.resilient_parsing:
            click.echo(ctx.get_help(), color=ctx.color)
            ctx.exit(0)
        return super().parse_args(ctx, args)


@dataclass
class GlobalOptions:
    """Root-level flags + injectable seams, carried on the click context object."""

    format: str | None = None
    query: str | None = None
    quiet: bool = False
    url: str | None = None
    configuration: str | None = None
    debug: bool = False
    no_paginate: bool = False
    store: Store = field(default_factory=Store)
    # Injectable client factory - tests hand in an ASGITransport-backed Client so
    # the REAL engine is exercised in-process. Default builds a live httpx Client.
    client_factory: Callable[[str, Any], Client] | None = None
    # Injectable output sink. Default None -> emit via click.echo (the terminal /
    # CliRunner buffer). Tests set a StringIO so command output is captured
    # deterministically, independent of pytest's fd-level stdout capture.
    out_stream: Any = None

    def emit(self, text: str, *, nl: bool = True) -> None:
        if self.out_stream is not None:
            self.out_stream.write(text + ("\n" if nl else ""))
        else:
            click.echo(text, nl=nl)

    def make_client(self, resolved: Resolved, *, retries: int | None = None) -> Client:
        if not resolved.url:
            raise DfeConfigError(
                "No engine URL configured. Run `dfe login --url <URL> ...` or pass "
                "--url / set DFE_API_URL."
            )
        # Warn if we are about to send a credential to a host that differs from the
        # profile's stored URL host (e.g. --url / $DFE_API_URL redirected the call).
        # Warn only - never refuse; the operator may legitimately be pointing at a
        # different endpoint with the same credential.
        if resolved.credential is not None and resolved.credential.value is not None:
            profile_url = self.store.get_value("url", resolved.profile)
            eff_host = urlsplit(resolved.url).netloc
            prof_host = urlsplit(profile_url).netloc if profile_url else ""
            if prof_host and eff_host and eff_host != prof_host:
                account = resolved.account or resolved.credential.account
                click.echo(
                    f"warning: sending {account} credentials to {eff_host}, which "
                    f"differs from the profile URL host {prof_host}",
                    err=True,
                )
        return self.client_for(resolved.url, resolved.credential, retries=retries)

    def client_for(self, url: str, credential: Any, *, retries: int | None = None) -> Client:
        """Build a client for an explicit url + credential (auth built-ins).

        ``retries`` is threaded to the client so non-idempotent verbs can disable
        the scalo retry policy (a retried POST/PUT/DELETE can double-mutate). None
        leaves the client default (3).
        """
        if self.client_factory is not None:
            return self.client_factory(url, credential)
        if retries is None:
            return Client(url, credential)
        return Client(url, credential, retries=retries)


def _param_name(cli_name: str) -> str:
    """Python identifier click will key the value under."""
    return cli_name.replace("-", "_")


def _option_for(spec: Param | BodyProp, *, required: bool) -> click.Option:
    """Build a click Option for a query param or body prop."""
    name = _param_name(spec.cli_name)
    help_text = spec.description or None
    if spec.type == "bool":
        return click.Option(
            [f"--{spec.cli_name}/--no-{spec.cli_name}", name],
            default=spec.default,
            required=required,
            help=help_text,
        )
    kwargs: dict[str, Any] = {
        "required": required,
        "help": help_text,
    }
    if spec.type == "int":
        kwargs["type"] = click.INT
    elif spec.type == "float":
        kwargs["type"] = click.FLOAT
    elif spec.type == "array":
        kwargs["multiple"] = True
    if spec.enum and spec.type == "str":
        kwargs["type"] = click.Choice([str(e) for e in spec.enum])
    if not required and spec.type != "array":
        kwargs["default"] = spec.default
    return click.Option([f"--{spec.cli_name}", name], **kwargs)


def _argument_for(param: Param) -> click.Argument:
    return click.Argument([_param_name(param.cli_name)], required=param.required)


def _default_format(global_opts: GlobalOptions, verb: str) -> str:
    if global_opts.format:
        return global_opts.format
    if not sys.stdout.isatty():
        return "json"  # script-safe default off a pipe
    if verb == "describe":
        return "yaml"
    if verb == "list":
        return "table"
    return "json"


def _confirm_destructive(op: Operation, global_opts: GlobalOptions) -> None:
    """Prompt before a delete; error out non-interactively unless --quiet."""
    if op.verb != "delete" or global_opts.quiet:
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        target = "/".join(op.group_path)
        if not click.confirm(f"Delete from {target}? This cannot be undone."):  # noqa: S608 - a terminal prompt, not SQL
            raise click.Abort()
    else:
        raise DfeConfigError(
            "Refusing to run a destructive command non-interactively without --quiet/-q."
        )


def _run(op: Operation, global_opts: GlobalOptions, kwargs: dict[str, Any]) -> None:
    """Execute one operation end to end (called from the click callback)."""
    resolved = resolve(
        global_opts.store,
        url_override=global_opts.url,
        profile_override=global_opts.configuration,
    )
    # Do not retry non-idempotent verbs - a retried POST/PUT/DELETE can double-
    # mutate. GET (list/describe) keeps the default retry policy.
    retries = None if op.method == "get" else 0
    client = global_opts.make_client(resolved, retries=retries)

    # Split the flat kwargs back into path/query/body by wire name.
    page_size = kwargs.pop("page_size", None)
    limit = kwargs.pop("limit", None)

    path_args = {p.name: kwargs.get(_param_name(p.cli_name)) for p in op.path_params}
    query_args = {
        p.name: kwargs.get(_param_name(p.cli_name))
        for p in op.query_params
        if kwargs.get(_param_name(p.cli_name)) is not None
    }
    if op.freeform_body:
        body_args = {"body": kwargs.get("body")}
    else:
        body_args = {
            p.name: kwargs.get(_param_name(p.cli_name))
            for p in op.body_props
            if kwargs.get(_param_name(p.cli_name)) is not None
        }

    _confirm_destructive(op, global_opts)

    try:
        if op.verb == "list" and paginate.is_paginated(op):
            data: Any = paginate.collect(
                client,
                op,
                path_args=path_args,
                query_args=query_args,
                page_size=page_size,
                limit=limit,
                no_paginate=global_opts.no_paginate,
            )
        else:
            data = client.call_json(
                op,
                path_args=path_args,
                query_args=query_args,
                body_args=body_args,
            )
    finally:
        client.close()

    if data is None:
        return  # 204 / empty body -> print nothing

    data = output.apply_query(data, global_opts.query)
    formatter = output.get_formatter(_default_format(global_opts, op.verb))
    # Render into a buffer then emit via the (injectable) sink.
    import io

    buffer = io.StringIO()
    formatter(data, buffer)
    global_opts.emit(buffer.getvalue(), nl=False)


def _make_callback(op: Operation) -> Callable[..., None]:
    @click.pass_context
    def callback(ctx: click.Context, **kwargs: Any) -> None:
        global_opts: GlobalOptions = ctx.obj
        try:
            _run(op, global_opts, kwargs)
        except click.Abort:
            raise
        except SystemExit:
            raise
        except BaseException as exc:
            emit = global_opts.emit if global_opts.out_stream is not None else None
            code = handle(exc, debug=global_opts.debug, emit=emit)
            ctx.exit(code)

    return callback


def _command_for(op: Operation) -> click.Command:
    params: list[click.Parameter] = [_argument_for(p) for p in op.path_params]
    # For a paginated `list` command the paginator drives page/per_page itself via
    # the injected --page-size / --limit knobs. Exposing the spec's own --page /
    # --per-page too would let `list --page 2` seed collect()'s start page and
    # silently drop earlier pages while auto-follow makes the output look complete.
    # So suppress those two spec query params here.
    suppress = {"page", "per_page"} if op.verb == "list" and paginate.is_paginated(op) else set()
    for qp in op.query_params:
        if qp.name in suppress:
            continue
        params.append(_option_for(qp, required=qp.required))
    if op.freeform_body:
        params.append(
            click.Option(
                ["--body"],
                required=True,
                help="Request body as a JSON string.",
            )
        )
    else:
        for prop in op.body_props:
            params.append(_option_for(prop, required=prop.required))

    if op.verb == "list":
        params.append(
            click.Option(
                ["--page-size", "page_size"],
                type=click.INT,
                help="Items per page (per_page query parameter).",
            )
        )
        params.append(
            click.Option(
                ["--limit", "limit"],
                type=click.INT,
                help="Stop after this many items (total cap).",
            )
        )

    help_text = op.summary or op.description or f"{op.method.upper()} {op.path}"
    return click.Command(
        name=op.verb,
        params=params,
        callback=_make_callback(op),
        help=help_text,
        short_help=op.summary or None,
    )


def _ensure_group(root: click.Group, path: list[str]) -> click.Group:
    """Walk/create the nested group chain for ``path`` and return the leaf."""
    current = root
    for segment in path:
        existing = current.commands.get(segment)
        if isinstance(existing, click.Group):
            current = existing
            continue
        if existing is not None:
            # Latent silent-drop guard: a leaf COMMAND already holds this name but
            # we now need a GROUP here (the reverse collision is guarded on the
            # command-add path). Displace the command to a disambiguated name
            # (mirrors the <verb>-<method> command-collision style) so no command
            # is ever lost, whatever order the spec is iterated in.
            del current.commands[segment]
            existing.name = f"{segment}-cmd"
            current.add_command(existing)
        new_group = HelpGroup(name=segment, no_args_is_help=True)
        current.add_command(new_group)
        current = new_group
    return current


def build_command_tree(root: click.Group, operations: list[Operation]) -> None:
    """Attach every operation to ``root`` as a generated command."""
    for op in operations:
        group = _ensure_group(root, op.group_path) if op.group_path else root
        command = _command_for(op)
        name = command.name
        if name in group.commands:
            # Two operations collided on the same (group, verb) - disambiguate by
            # method so neither command is lost.
            name = f"{op.verb}-{op.method}"
            command.name = name
        group.add_command(command)
