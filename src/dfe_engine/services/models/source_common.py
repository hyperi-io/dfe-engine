"""Shared models for multi-source service configurations.

Services that support multiple source deployments (transform-vector,
transform-wasm, fetcher) share the pattern of per-source ENV overrides
and associated files. This module provides the common base.

The per-source ENV + files pattern is CRUD-managed via the engine API,
called by the control plane REST API.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SourceFileConfig(BaseModel):
    """An associated file for a source deployment.

    Files (enrichment CSV/MMDB, config YAML, WASM modules, etc.) are
    mounted into the container and referenced by the source config.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., description="Path inside the container")
    type: str = Field(
        default="auto",
        description="File type: auto, csv, mmdb, json, yaml, wasm (auto-detected from extension)",
    )
    source: str = Field(
        default="",
        description="Source location: ConfigMap name, S3 URI, or local path for dev",
    )
    reload_interval_secs: int = Field(
        default=3600, ge=0, description="How often to check for file updates (0 = never)"
    )


class BaseSourceConfig(BaseModel):
    """Base for per-source deployment configs.

    Every source deployment (across transform-vector, transform-wasm, fetcher)
    has a name, associated ENV overrides, associated files, and enabled state.
    Service-specific fields (config_file, wasm_module, source_type, etc.)
    are added in subclasses.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique source name within this deployment")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables for this source",
    )
    files: list[SourceFileConfig] = Field(
        default_factory=list,
        description="Associated files for this source (enrichment, config, etc.)",
    )
    enabled: bool = True
    description: str = ""
