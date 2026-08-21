#  Project:      dfe-engine
#  File:         hyperdx/dashboards/__init__.py
#  Purpose:      SSoT for the pre-canned HyperDX dashboards shipped with a DFE deploy
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The pre-canned HyperDX dashboards, and the code that materialises them.

The engine is the SSoT for dashboard CONTENT; the deploy layer only carries it.
The ``.json`` files beside this module are HyperDX **Template** dashboards -- they
reference sources by NAME (``"source": "default"``), not by the per-team ObjectId a
Document dashboard carries, because there is no id to write until a team exists.

How a file reaches a running HyperDX:

1. ``python -m dfe_engine.hyperdx.dashboards --out DIR`` writes them to ``DIR``.
   dfe-infra runs that as an init container beside the HyperDX pod; dfe-docker runs
   it as a one-shot compose service. Both share the engine image, so there is one
   copy of the JSON in the whole product.
2. HyperDX's own ``provision-dashboards`` task reads ``DASHBOARD_PROVISIONER_DIR``
   on a one-minute cron, resolves each source NAME against the team's sources, and
   upserts a dashboard marked ``provisioned``.

Two consequences follow from that reconcile loop, and both are deliberate:

- **Provisioned dashboards are read-only.** The task ``$set``s tiles every minute,
  so a user edit would silently revert inside 60s. The fork 403s writes to them and
  users take their own copy instead (Export -> Import).
- **A dashboard is seeded only where it resolves.** With
  ``DASHBOARD_PROVISIONER_REQUIRE_REFS=true`` a dashboard naming a source the team
  does not hold is skipped for that team. That is what keeps operator telemetry off
  a tenant: only the platform team holds ``otel_metrics``, so only the platform team
  gets the platform dashboards. There is no allow-list to maintain -- the source set
  IS the audience.

Two kinds of tile appear here. A BUILDER tile names a source and lets HyperDX
compose the SQL. A RAW-SQL tile (``"configType": "sql"``) carries its own
``sqlTemplate`` and names a ``connection`` as well, which is what the ClickHouse
dashboards use: they read ClickHouse's ``system`` tables directly, which no source
models. Their fence is the same mechanism -- only the platform team holds a
connection named ``platform`` -- and it is doubled at the data layer, since only
the platform ClickHouse reader is granted ``system``.

Raw SQL binds to the dashboard's time range through macros, expanded by HyperDX
before execution: ``$__dateTimeFilter(event_date, event_time)`` for ClickHouse's
partitioned system logs, ``$__timeFilter(col)`` elsewhere, and ``$__interval_s``
for the bucket width. A time-series tile must return a Date/DateTime column, so
never cast the bucket to an integer -- the chart infers its x-axis from the column
TYPE and draws nothing for an Int.

Adding a dashboard is dropping a ``.json`` file in this directory. Nothing indexes
them by name, so no registry to update.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

__all__ = [
    "DASHBOARD_SUFFIX",
    "dashboard_files",
    "dashboard_names",
    "export_dashboards",
]

DASHBOARD_SUFFIX = ".json"


def dashboard_files() -> dict[str, str]:
    """Return ``{filename: json_text}`` for every dashboard shipped with the engine.

    Reads through ``importlib.resources`` rather than ``__file__`` so it works from a
    zipimport or any other non-filesystem loader.

    Returns:
        Mapping of file name (e.g. ``dfe-throughput.json``) to its raw JSON text,
        sorted by name so the output is stable across calls and platforms.

    Raises:
        ValueError: A shipped file is not valid JSON. This is a packaging fault, not
            a runtime condition -- the provisioner would silently skip the file, so
            failing here turns it into a test failure instead of a missing dashboard.
    """
    contents: dict[str, str] = {}
    for entry in sorted(resources.files(__package__).iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(DASHBOARD_SUFFIX) or not entry.is_file():
            continue
        text = entry.read_text(encoding="utf-8")
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{entry.name} is not valid JSON: {exc}") from exc
        contents[entry.name] = text
    return contents


def dashboard_names() -> list[str]:
    """Return the ``name`` field of every shipped dashboard, in file order.

    The name is the provisioner's upsert key (``{name, team, provisioned}``), so it
    is the identity a deploy actually sees. Exposed for tests and for an operator
    asking what a given engine version would seed.
    """
    return [json.loads(text)["name"] for text in dashboard_files().values()]


def export_dashboards(out_dir: str | Path) -> list[Path]:
    """Write every shipped dashboard into ``out_dir``, creating it if absent.

    Overwrites unconditionally: the engine version is the SSoT, so whatever is on
    disk from an older image is stale by definition. Files in ``out_dir`` that the
    engine does not ship are left alone, so an operator can drop a site-local
    dashboard into the same directory without it being swept away on restart.

    Args:
        out_dir: Directory to write into (the provisioner's
            ``DASHBOARD_PROVISIONER_DIR``).

    Returns:
        The paths written, sorted by name.
    """
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name, text in dashboard_files().items():
        path = target / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written
