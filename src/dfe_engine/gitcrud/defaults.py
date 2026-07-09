#  Project:      dfe-engine
#  File:         gitcrud/defaults.py
#  Purpose:      Chart-default diff for Tier-1 helm vars (changed-from-default view)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Annotate flattened overlay vars against chart defaults.

Given a resource's current flattened vars and the chart's default values doc, mark
each var with its default and whether it has been changed - the "big dials changed
from default" view the UI wants. The chart-defaults source (a chart values.yaml)
is supplied by the caller, so this stays a pure function.
"""

from __future__ import annotations

from typing import Any

from .engine import flatten


def diff_against_defaults(
    current_flat: dict[str, Any],
    chart_defaults: dict | None,
) -> list[dict[str, Any]]:
    """Return [{path, value, default, changed}] for every current var.

    A var is ``changed`` when it has no default or differs from it. Vars present in
    the defaults but not the overlay are NOT listed (the overlay is the override set).
    """
    defaults_flat = flatten(chart_defaults or {})
    out: list[dict[str, Any]] = []
    for path, value in current_flat.items():
        default = defaults_flat.get(path)
        out.append(
            {
                "path": path,
                "value": value,
                "default": default,
                "changed": default is None or default != value,
            }
        )
    return out
