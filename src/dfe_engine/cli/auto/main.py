#  Project:      dfe-engine
#  File:         cli/auto/main.py
#  Purpose:      Console-script entry point for the generated `dfe` CLI
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe`` console-script entry point (``[project.scripts] dfe``)."""

from .app import build_cli


def main() -> None:
    """Build the command tree and dispatch."""
    build_cli().main()


if __name__ == "__main__":
    main()
