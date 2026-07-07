"""System router — version, settings summary.

GET /api/v1/system/version     → Version info
GET /api/v1/system/settings    → Redacted settings summary
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine.api.cli_exposure import CLI_HIDDEN
from dfe_engine.api.deps import CurrentUser, Settings, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/system", tags=["System"])


# ── Response models ──────────────────────────────────────────


class VersionResponse(BaseModel):
    version: str = Field(description="Package version")
    python_version: str = Field(description="Python interpreter version")


class SettingsSummary(BaseModel):
    """Redacted settings — no secrets."""

    clickhouse_host: str
    clickhouse_database: str
    clickhouse_data_database: str
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
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
async def get_settings(user: CurrentUser, settings: Settings):
    """Get a redacted summary of current settings. No secrets exposed."""
    return SettingsSummary(
        clickhouse_host=settings.clickhouse.host,
        clickhouse_database=settings.clickhouse.database,
        clickhouse_data_database=settings.clickhouse.effective_data_database,
        sources_dir=settings.source.sources_dir,
        services_config_dir=settings.services.config_yaml_dir,
        hunt_dir=settings.hunts.hunt_dir,
        auth_enabled=settings.auth.enabled,
        auth_local_enabled=settings.auth.local.enabled,
        api_host=settings.api.host,
        api_port=settings.api.port,
        api_cors_origins=settings.api.cors_origins,
    )


# ── ClickHouse Cloud lifecycle (control plane) ───────────────


class CloudServiceStateResponse(BaseModel):
    """CH Cloud service control-plane state (see docs/CLICKHOUSE-CLOUD.md)."""

    configured: bool = Field(description="Whether the CH Cloud control-plane key is set.")
    id: str = Field(default="", description="CH Cloud service id.")
    name: str = Field(default="", description="CH Cloud service name.")
    state: str = Field(
        default="", description="running / stopped / idle / starting / stopping / ..."
    )
    is_running: bool = Field(default=False, description="True when the service is running.")


def _cloud_state(settings) -> CloudServiceStateResponse:
    from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError

    cloud = settings.clickhouse.cloud
    if not cloud.configured:
        return CloudServiceStateResponse(configured=False)
    try:
        st = CloudService(cloud).status()
    except CloudServiceError as exc:
        raise HTTPException(
            status_code=502, detail={"code": "cloud_error", "message": str(exc)}
        ) from exc
    return CloudServiceStateResponse(
        configured=True, id=st.id, name=st.name, state=st.state, is_running=st.is_running
    )


@router.get(
    "/clickhouse-cloud",
    response_model=CloudServiceStateResponse,
    dependencies=[Depends(require_action(scopes_dict["system_read"]))],
)
def clickhouse_cloud_status(user: CurrentUser, settings: Settings):
    """CH Cloud service control-plane status (read-only)."""
    return _cloud_state(settings)


@router.post(
    "/clickhouse-cloud/start",
    response_model=CloudServiceStateResponse,
    dependencies=[Depends(require_action(scopes_dict["clickhouse_cloud_manage"]))],
)
def clickhouse_cloud_start(user: CurrentUser, settings: Settings):
    """Start (wake) the CH Cloud service. BILLABLE + admin-gated + audited."""
    from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError

    cloud = settings.clickhouse.cloud
    if not cloud.configured:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "ClickHouse Cloud is not configured"},
        )
    logger.info("CH Cloud start requested via API", actor=getattr(user, "username", "?"))
    try:
        st = CloudService(cloud).start()
    except CloudServiceError as exc:
        raise HTTPException(
            status_code=502, detail={"code": "cloud_error", "message": str(exc)}
        ) from exc
    return CloudServiceStateResponse(
        configured=True, id=st.id, name=st.name, state=st.state, is_running=st.is_running
    )


@router.post(
    "/clickhouse-cloud/stop",
    response_model=CloudServiceStateResponse,
    dependencies=[Depends(require_action(scopes_dict["clickhouse_cloud_manage"]))],
)
def clickhouse_cloud_stop(user: CurrentUser, settings: Settings):
    """Stop the CH Cloud service (saves cost). Admin-gated + audited."""
    from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError

    cloud = settings.clickhouse.cloud
    if not cloud.configured:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "ClickHouse Cloud is not configured"},
        )
    logger.info("CH Cloud stop requested via API", actor=getattr(user, "username", "?"))
    try:
        st = CloudService(cloud).stop()
    except CloudServiceError as exc:
        raise HTTPException(
            status_code=502, detail={"code": "cloud_error", "message": str(exc)}
        ) from exc
    return CloudServiceStateResponse(
        configured=True, id=st.id, name=st.name, state=st.state, is_running=st.is_running
    )


class HuntsDueResponse(BaseModel):
    due: int = Field(description="Count of hunts due to run now (the KEDA scaling metric)")


@router.get(
    "/hunts-due",
    response_model=HuntsDueResponse,
    dependencies=[Depends(require_action(scopes_dict["hunt_read"]))],
    openapi_extra=CLI_HIDDEN,
)
def hunts_due(user: CurrentUser, settings: Settings) -> HuntsDueResponse:
    """The deterministic hunt backlog count - the hunt-runner autoscaling metric.

    KEDA's stock ``metrics-api`` scaler polls this (``valueLocation: due``) to scale
    the hunt-runner, so KEDA NEVER needs a ClickHouse wire port (no ``mysql_port`` /
    ``postgresql_port`` on CH) and no extra scaler component: the engine already
    holds the CH HTTP connection and is the authority on what is due, so it just
    runs the deterministic due-query and returns the count. Fails SAFE to ``due=0``
    on a transient CH error - a scaling metric must never spuriously scale UP (or
    block scale-to-zero) because the count call blipped.
    """
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.hunt_runner.schedule import due_count
    from dfe_engine.settings import get_clickhouse_config

    try:
        ch = ClickHouseManager.get_instance(get_clickhouse_config(settings)).get_clickhouse_client()
        return HuntsDueResponse(due=due_count(ch, settings.clickhouse.effective_data_database))
    except Exception:
        logger.warning("hunts-due metric unavailable; reporting due=0", exc_info=True)
        return HuntsDueResponse(due=0)


# ── Helpers ──────────────────────────────────────────────────


def _get_version() -> str:
    try:
        from importlib.metadata import version

        return version("dfe-engine")
    except Exception:
        return "dev"
