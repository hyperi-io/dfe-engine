#  Project:      dfe-engine
#  File:         cli/auto/main.py
#  Purpose:      Console-script entry point for the generated `dfe` CLI
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe`` console-script entry point (``[project.scripts] dfe``)."""

from __future__ import annotations

from .app import make_root


def main() -> None:
    """Build the root command tree and dispatch."""
    import typer

    root = make_root()
    try:
        root()
    except typer.Exit as exc:
        # typer 0.26 vendors its own click, so a mounted typer sub-app's
        # ``typer.Exit`` (raised by e.g. ``dfe local ch-cloud``) is a DIFFERENT
        # class from the ``click.Exit`` the real-click root group catches - it
        # would otherwise escape uncaught and dump a traceback. Translate it into
        # a normal process exit (the sub-app already printed its message).
        raise SystemExit(exc.exit_code) from None


if __name__ == "__main__":
    main()
