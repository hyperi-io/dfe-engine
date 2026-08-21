#  Project:      dfe-engine
#  File:         hyperdx/dashboards/__main__.py
#  Purpose:      CLI that materialises the shipped HyperDX dashboards into a directory
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``python -m dfe_engine.hyperdx.dashboards`` -- write the dashboards to a directory.

Deliberately a module entry point rather than a ``[project.scripts]`` console script:
the callers are an init container and a one-shot compose service that already run the
engine image, and a module needs no packaging change to reach.

Usage::

    python -m dfe_engine.hyperdx.dashboards --out /etc/hyperdx/dashboards
    python -m dfe_engine.hyperdx.dashboards --list

Imports nothing from the rest of the engine, so it starts in a container with no
config, no secrets store and no database -- which is exactly the environment an init
container runs in.
"""

from __future__ import annotations

import argparse
import json
import sys

from dfe_engine.hyperdx.dashboards import dashboard_files, export_dashboards


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and export (or list) the shipped dashboards.

    Returns:
        Process exit code: 0 on success, 2 on a usage error, 1 on a write failure.
    """
    parser = argparse.ArgumentParser(
        prog="python -m dfe_engine.hyperdx.dashboards",
        description="Write the DFE HyperDX dashboards into a provisioner directory.",
    )
    parser.add_argument(
        "--out",
        metavar="DIR",
        help="directory to write the dashboard JSON into (created if absent)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the dashboard file names and titles instead of writing",
    )
    args = parser.parse_args(argv)

    if args.list:
        for name, text in dashboard_files().items():
            print(f"{name}\t{json.loads(text)['name']}")
        return 0

    if not args.out:
        parser.error("one of --out or --list is required")

    try:
        written = export_dashboards(args.out)
    except OSError as exc:
        print(f"failed to write dashboards to {args.out}: {exc}", file=sys.stderr)
        return 1

    # The init container's logs are the only record that provisioning input arrived,
    # so name every file rather than printing a count.
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
