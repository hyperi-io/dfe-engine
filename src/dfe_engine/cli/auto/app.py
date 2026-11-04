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

import dataclasses
import json
import os
import re
import secrets
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from scalo.cli.openapi import Response, ScaloHttpTransport, SpecCli, Store, Transport

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


def _envelope(reply: Response) -> dict[str, Any] | None:
    """The reply body when it is the engine's page-number envelope, else ``None``."""
    try:
        body = reply.json()
    except ValueError:
        return None
    paged = (
        isinstance(body, dict)
        and isinstance(body.get("items"), list)
        and {"page", "next_page"} <= body.keys()
    )
    return body if paged else None


class _PageWalker:
    """Transport that answers a paged list with its ``items``, from every page unless the request names ``page``.

    The engine pages by number (``items``, ``next_page``), where scalo pages by ``page_token``.
    """

    def __init__(self, inner: Transport) -> None:
        self._inner = inner

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        credentials: Mapping[str, str],
        content: bytes | None,
        timeout: float,
    ) -> Response:
        """Send the request, then every later page of a paged list."""
        reply = self._inner.send(
            method, url, headers=headers, credentials=credentials, content=content, timeout=timeout
        )
        body = _envelope(reply) if method == "GET" and reply.ok else None
        if body is None:
            return reply
        items = list(body["items"])
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        # A page the reply names must be later than the one just asked for, or a looping server never ends the walk.
        asked = body["page"]
        while (
            all(name != "page" for name, _ in pairs)
            and isinstance(body.get("next_page"), int)
            and body["next_page"] > asked
        ):
            asked = body["next_page"]
            next_url = parts._replace(query=urlencode([*pairs, ("page", asked)])).geturl()
            reply = self._inner.send(
                method,
                next_url,
                headers=headers,
                credentials=credentials,
                content=None,
                timeout=timeout,
            )
            body = _envelope(reply) if reply.ok else None
            if body is None:
                return reply
            items.extend(body["items"])
        return dataclasses.replace(reply, content=json.dumps(items).encode())


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
        transport=_PageWalker(transport or ScaloHttpTransport()),
        store=store,
    )
    attach_builtins(cli)
    cli.add_typer(local_app, "local")
    cli.add_typer(schema_app, "schema")
    cli.command("kafka", "client-config")(client_config)
    return cli
