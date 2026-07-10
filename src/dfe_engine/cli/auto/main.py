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
    root = make_root()
    root()


if __name__ == "__main__":
    main()
