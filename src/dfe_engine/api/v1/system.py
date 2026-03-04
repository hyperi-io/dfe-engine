"""System router — version, settings summary.

GET /api/v1/system/version     → Version info
GET /api/v1/system/settings    → Redacted settings summary
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, Settings, require_action

router = APIRouter(prefix="/system", tags=["System"])


# ── Response models ──────────────────────────────────────────


class VersionResponse(BaseModel):
    version: str = Field(description="Package version")
    python_version: str = Field(description="Python interpreter version")


class SettingsSummary(BaseModel):
    """Redacted settings — no secrets."""

    clickhouse_host: str
    clickhouse_database: str
    sources_dir: str
    services_config_dir: str
    hunt_dir: str
    auth_enabled: bool
    auth_local_enabled: bool
    api_host: str
    api_port: int
    api_cors_origins: list[str]


# ── Endpoints ────────────────────────────────────────────────


@router.get("/version", response_model=VersionResponse)
async def get_version(user: CurrentUser):
    """Get engine version info."""
    import sys

    return VersionResponse(
        version=_get_version(),
        python_version=sys.version.split()[0],
    )


@router.get(
    "/settings",
    response_model=SettingsSummary,
    dependencies=[Depends(require_action("config:read"))],
)
async def get_settings(user: CurrentUser, settings: Settings):
    """Get a redacted summary of current settings. No secrets exposed."""
    return SettingsSummary(
        clickhouse_host=settings.clickhouse.host,
        clickhouse_database=settings.clickhouse.database,
        sources_dir=settings.source.sources_dir,
        services_config_dir=settings.services.config_yaml_dir,
        hunt_dir=settings.hunts.hunt_dir,
        auth_enabled=settings.auth.enabled,
        auth_local_enabled=settings.auth.local.enabled,
        api_host=settings.api.host,
        api_port=settings.api.port,
        api_cors_origins=settings.api.cors_origins,
    )


# ── Helpers ──────────────────────────────────────────────────


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("dfe-engine")
    except Exception:
        return "dev"
