#  Project:      dfe-engine
#  File:         gitops/support_drift.py
#  Purpose:      SUPPORT-DRIFT startup notice for per-component version overrides
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""SUPPORT-DRIFT: surface per-component version overrides at engine startup.

The deploy repo's ``pins.yaml`` selects ONE certified stack version; its
``overrides`` block moves individual components off that set at the operator's
own risk (dfe-docs/deployment/stack-versioning.md). The engine cannot know the
certified values at runtime (they live in dfe-infra at the pinned tag), but it
CAN make the deviation loud: every component listed under ``overrides`` is an
untested combination, so name each one at startup, every startup.
"""

from __future__ import annotations

from pathlib import Path

from scalo.logger import logger

from dfe_engine.yaml_utils import yaml_load

PINS_FILE = "pins.yaml"


def support_drift_overrides(deploy_repo_path: str | Path) -> dict[str, str]:
    """The ``overrides`` component map from the deploy repo's pins.yaml.

    Flattens the per-group blocks (``apps``, ...) into ``name -> ref``.
    Returns empty when there is no pins.yaml, no overrides, or the file is
    unreadable -- the notice must never break startup.
    """
    path = Path(deploy_repo_path) / PINS_FILE
    if not path.is_file():
        return {}
    try:
        data = yaml_load(path)
    except Exception:  # malformed pins.yaml is the operator's problem, not a crash
        logger.warning("SUPPORT-DRIFT check skipped: could not parse %s", path)
        return {}
    if not isinstance(data, dict):
        return {}
    overrides = data.get("overrides")
    if not isinstance(overrides, dict):
        return {}
    flat: dict[str, str] = {}
    for group in overrides.values():
        if isinstance(group, dict):
            for name, ref in group.items():
                flat[str(name)] = str(ref)
    return flat


def log_support_drift(deploy_repo_path: str | Path) -> dict[str, str]:
    """Log the SUPPORT-DRIFT notice for any pins.yaml overrides; returns them."""
    overrides = support_drift_overrides(deploy_repo_path)
    if overrides:
        logger.warning(
            "SUPPORT-DRIFT: this environment runs an UNTESTED combination - "
            "%d component(s) overridden off the certified stack (pins.yaml): %s",
            len(overrides),
            ", ".join(f"{name}={ref}" for name, ref in sorted(overrides.items())),
        )
    return overrides
