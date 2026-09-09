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

from .pins import component_overrides, load_pins


def log_support_drift(deploy_repo_path: str | Path) -> dict[str, str]:
    """Log the SUPPORT-DRIFT notice for any pins.yaml overrides; returns them."""
    overrides = component_overrides(load_pins(deploy_repo_path))
    if overrides:
        listed = ", ".join(f"{name}={ref}" for name, ref in sorted(overrides.items()))
        logger.warning(
            f"SUPPORT-DRIFT: this environment runs an UNTESTED combination - "
            f"{len(overrides)} component(s) overridden off the certified stack "
            f"(pins.yaml): {listed}"
        )
    return overrides
