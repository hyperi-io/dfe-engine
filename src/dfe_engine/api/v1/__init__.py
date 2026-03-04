"""DFE Engine API v1 router assembly."""

from fastapi import APIRouter

from dfe_engine.api.v1.alerts import router as alerts_router
from dfe_engine.api.v1.auth import router as auth_router
from dfe_engine.api.v1.deployments import router as deployments_router
from dfe_engine.api.v1.fieldmaps import router as fieldmaps_router
from dfe_engine.api.v1.rules import router as rules_router
from dfe_engine.api.v1.services import router as services_router
from dfe_engine.api.v1.sources import router as sources_router
from dfe_engine.api.v1.system import router as system_router

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(auth_router)
v1_router.include_router(sources_router)
v1_router.include_router(services_router)
v1_router.include_router(deployments_router)
v1_router.include_router(fieldmaps_router)
v1_router.include_router(rules_router)
v1_router.include_router(alerts_router)
v1_router.include_router(system_router)

__all__ = ["v1_router"]
