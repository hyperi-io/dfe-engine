#  Project:      dfe-engine
#  File:         cli/auto/exit.py
#  Purpose:      One way for the daemon-free commands to report a failure
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Failure exit for the commands that run without an engine (``local``, ``schema``, ``kafka``)."""

from typing import NoReturn

import typer


def fail(message: str) -> NoReturn:
    """Print ``message`` on stderr and exit 1."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)
