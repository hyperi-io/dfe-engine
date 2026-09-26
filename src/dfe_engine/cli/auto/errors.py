#  Project:      dfe-engine
#  File:         cli/auto/errors.py
#  Purpose:      Exit-code taxonomy + friendly error mapping for the dfe CLI
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Exit codes and error->stderr mapping for the ``dfe`` CLI.

Codes (aws-cli/gcloud shaped):

    0    ok
    2    usage (click's own bad-args exit)
    252  parameter / validation error (4xx up to and including 422)
    253  config / no-credentials
    254  server / API error (other 4xx and all 5xx)
    255  general / unexpected

The engine speaks a unified ``ErrorResponse`` body ``{code, message, ...}`` on
failure; we render it as ``Error (<code>): <message>``. ``--debug`` re-raises so
the full traceback prints.
"""

from __future__ import annotations

import sys
from enum import IntEnum
from typing import Any

import click
import httpx


class ExitCode(IntEnum):
    OK = 0
    USAGE = 2
    VALIDATION = 252
    CONFIG = 253
    API = 254
    GENERAL = 255


class DfeCliError(click.ClickException):
    """A handled CLI error carrying an exit code + a user-facing message.

    Named ``Dfe...`` to avoid shadowing ``scalo.cli.CliError`` (a distinct type
    also in scope). Subclasses ``click.ClickException`` so a raise anywhere
    (generated command or hand-written built-in) is caught by click's standalone
    mode, printed to stderr and exits with our taxonomy code - no bespoke wrapping.
    """

    def __init__(self, message: str, exit_code_value: ExitCode = ExitCode.GENERAL) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = int(exit_code_value)

    def show(self, file: object = None) -> None:  # override: no extra "Error: " prefix
        click.echo(self.message, file=file or sys.stderr)


class DfeConfigError(DfeCliError):
    """Missing/invalid config or credentials (exit 253).

    Named ``Dfe...`` to avoid shadowing ``scalo.cli.ConfigError``.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, ExitCode.CONFIG)


def _extract_error_body(response: httpx.Response) -> tuple[str, str]:
    """Pull ``(code, message)`` from an ErrorResponse-shaped body, best effort."""
    code = str(response.status_code)
    message = response.reason_phrase or "request failed"
    try:
        body = response.json()
    except ValueError, httpx.DecodingError:
        return code, message
    if isinstance(body, dict):
        # ErrorResponse: {code, message}. FastAPI HTTPException: {detail: {...}}
        # or {detail: "..."}.
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            code = str(detail.get("code", code))
            message = str(detail.get("message", detail.get("detail", message)))
        elif isinstance(detail, list):
            # 422 validation error list.
            parts = []
            for item in detail:
                loc = ".".join(str(x) for x in item.get("loc", [])[1:])
                parts.append(f"{loc}: {item.get('msg', '')}".strip(": "))
            message = "; ".join(parts) or message
            code = "validation_error"
        else:
            message = str(detail)
            code = str(body.get("code", code))
    return code, message


def from_http_status_error(exc: httpx.HTTPStatusError) -> DfeCliError:
    """Build a DfeCliError from a non-2xx response."""
    response = exc.response
    code, message = _extract_error_body(response)
    status = response.status_code
    if status == 422:
        exit_code = ExitCode.VALIDATION
    elif 400 <= status < 500 or 500 <= status < 600:
        exit_code = ExitCode.API
    else:
        exit_code = ExitCode.GENERAL
    return DfeCliError(f"Error ({code}): {message}", exit_code)


def handle(exc: BaseException, *, debug: bool = False, emit: Any = None) -> int:
    """Print a friendly error line to stderr and return the process exit code.

    With ``debug`` the original exception is re-raised so the traceback prints.
    ``emit`` is an optional ``callable(text)`` sink (tests inject one); by default
    the line goes to click's stderr stream (captured by CliRunner).
    """
    if debug:
        raise exc

    def _write(text: str) -> None:
        if emit is not None:
            emit(text)
        else:
            click.echo(text, err=True)

    if isinstance(exc, httpx.HTTPStatusError):
        cli = from_http_status_error(exc)
        _write(cli.message)
        return int(cli.exit_code)

    if isinstance(exc, DfeCliError):
        _write(cli_message(exc))
        return int(exc.exit_code)

    if isinstance(exc, httpx.ConnectError):
        _write(f"Error (connection): could not reach the engine ({exc}).")
        return int(ExitCode.API)

    if isinstance(exc, httpx.HTTPError):
        _write(f"Error (http): {exc}")
        return int(ExitCode.API)

    _write(f"Error: {exc}")
    return int(ExitCode.GENERAL)


def cli_message(exc: DfeCliError) -> str:
    msg: Any = exc.message
    if not str(msg).startswith("Error"):
        return f"Error: {msg}"
    return str(msg)
