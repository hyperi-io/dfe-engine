#  Project:      dfe-engine
#  File:         surfaces/models.py
#  Purpose:      Pydantic models for Rust service surface definitions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Service surface models -- config settings + metrics exposed by Rust services.

Each service surface describes what a Rust service exposes:
- config_surface: settings the UI can edit (pushed via Helm values)
- metrics_surface: metrics the service emits (from DFE metrics standard)
- manifest_url: endpoint for live metric manifest discovery (Phase 1.5)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConfigSurfaceEntry(BaseModel):
    """A single configurable setting for a Rust service."""

    type: str = "string"
    description: str = ""
    default: Any = None


class MetricEntry(BaseModel):
    """A single metric from the service's manifest."""

    name: str
    type: str  # counter, gauge, histogram
    description: str = ""
    unit: str = ""
    labels: list[str] = Field(default_factory=list)
    group: str = ""  # app, buffer, consumer, sink, etc.


class ServiceSurface(BaseModel):
    """Complete surface definition for a Rust service.

    Loaded from YAML files in the service-surfaces config directory.
    One file per service (e.g. ``dfe-receiver.yaml``).
    """

    service: str
    description: str = ""
    config_surface: dict[str, ConfigSurfaceEntry] = Field(default_factory=dict)
    metrics_surface: list[MetricEntry] = Field(default_factory=list)
    manifest_url: str = ""
    discovered_at: str = ""
