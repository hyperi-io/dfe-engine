"""DFE Engine API v1 router assembly."""

from fastapi import APIRouter

from dfe_engine.api.v1.account_groups import router as account_groups_router
from dfe_engine.api.v1.accounts import router as accounts_router
from dfe_engine.api.v1.alerts import router as alerts_router
from dfe_engine.api.v1.api_keys import router as api_keys_router
from dfe_engine.api.v1.auth import router as auth_router
from dfe_engine.api.v1.cel import router as cel_router
from dfe_engine.api.v1.deployments import router as deployments_router
from dfe_engine.api.v1.discovery import router as discovery_router
from dfe_engine.api.v1.fieldmaps import router as fieldmaps_router
from dfe_engine.api.v1.hunts import router as hunts_router
from dfe_engine.api.v1.oidc_providers import router as oidc_providers_router
from dfe_engine.api.v1.orgs import router as orgs_router
from dfe_engine.api.v1.pipeline import router as pipeline_router
from dfe_engine.api.v1.queries import router as queries_router
from dfe_engine.api.v1.rules import router as rules_router
from dfe_engine.api.v1.schemas import router as schemas_router
from dfe_engine.api.v1.service_surfaces import router as service_surfaces_router
from dfe_engine.api.v1.services import router as services_router
from dfe_engine.api.v1.sigma import router as sigma_router
from dfe_engine.api.v1.sources import router as sources_router
from dfe_engine.api.v1.system import router as system_router
from dfe_engine.api.v1.tasks import router as tasks_router
from dfe_engine.api.v1.transforms import router as transforms_router

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(auth_router)

# Auth sub-routers mounted under /auth prefix
_auth_sub = APIRouter(prefix="/auth")
_auth_sub.include_router(accounts_router)
_auth_sub.include_router(account_groups_router)
_auth_sub.include_router(api_keys_router)
_auth_sub.include_router(oidc_providers_router)
v1_router.include_router(_auth_sub)

v1_router.include_router(orgs_router)
v1_router.include_router(sources_router)
v1_router.include_router(services_router)
v1_router.include_router(deployments_router)
v1_router.include_router(fieldmaps_router)
v1_router.include_router(rules_router)
v1_router.include_router(alerts_router)
v1_router.include_router(service_surfaces_router)
v1_router.include_router(system_router)
v1_router.include_router(transforms_router)
v1_router.include_router(hunts_router)
v1_router.include_router(queries_router)
v1_router.include_router(pipeline_router)
v1_router.include_router(tasks_router)
v1_router.include_router(discovery_router)
v1_router.include_router(schemas_router)
v1_router.include_router(sigma_router)
v1_router.include_router(cel_router)

__all__ = ["v1_router"]
