#  Project:      dfe-engine
#  File:         surfaces/__init__.py
#  Purpose:      Schema-less Rust service surface discovery
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Service surface discovery -- YAML-defined config + metrics for Rust services.

Adding a new dfe-* Rust service = adding a YAML surface file.  Zero Python code.
"""

from __future__ import annotations

from dfe_engine.services.surfaces.models import (
    ConfigSurfaceEntry,
    MetricEntry,
    ServiceSurface,
)
from dfe_engine.services.surfaces.registry import SurfaceRegistry

__all__ = [
    "ConfigSurfaceEntry",
    "MetricEntry",
    "ServiceSurface",
    "SurfaceRegistry",
]
