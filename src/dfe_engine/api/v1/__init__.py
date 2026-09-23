"""DFE Engine API v1 router assembly."""

from fastapi import APIRouter, Depends, Request

from dfe_engine.api.v1.account_groups import router as account_groups_router
from dfe_engine.api.v1.accounts import router as accounts_router
from dfe_engine.api.v1.alerts import router as alerts_router
from dfe_engine.api.v1.api_keys import router as api_keys_router
from dfe_engine.api.v1.app_contracts import router as app_contracts_router
from dfe_engine.api.v1.apps import router as apps_router
from dfe_engine.api.v1.auth import router as auth_router
from dfe_engine.api.v1.authoring import router as authoring_router
from dfe_engine.api.v1.backing_services import router as backing_services_router
from dfe_engine.api.v1.cel import router as cel_router
from dfe_engine.api.v1.config import router as config_router
from dfe_engine.api.v1.deployments import router as deployments_router
from dfe_engine.api.v1.derived_schemas import router as derived_schemas_router
from dfe_engine.api.v1.discovery import router as discovery_router
from dfe_engine.api.v1.fieldmaps import router as fieldmaps_router
from dfe_engine.api.v1.gitops import router as gitops_router
from dfe_engine.api.v1.governance import router as governance_router
from dfe_engine.api.v1.helm import router as helm_router
from dfe_engine.api.v1.hunts import router as hunts_router
from dfe_engine.api.v1.hyperdx import router as hyperdx_router
from dfe_engine.api.v1.kafka_topics import router as kafka_topics_router
from dfe_engine.api.v1.library import router as library_router
from dfe_engine.api.v1.lifecycle import router as lifecycle_router
from dfe_engine.api.v1.oidc_login import router as oidc_login_router
from dfe_engine.api.v1.oidc_providers import router as oidc_providers_router
from dfe_engine.api.v1.orgs import router as orgs_router
from dfe_engine.api.v1.pipeline import router as pipeline_router
from dfe_engine.api.v1.queries import router as queries_router
from dfe_engine.api.v1.repository import router as repository_router
from dfe_engine.api.v1.roles import router as roles_router
from dfe_engine.api.v1.rules import router as rules_router
from dfe_engine.api.v1.sampler import router as sampler_router
from dfe_engine.api.v1.schemas import router as schemas_router
from dfe_engine.api.v1.scim import router as scim_router
from dfe_engine.api.v1.service_surfaces import router as service_surfaces_router
from dfe_engine.api.v1.services import router as services_router
from dfe_engine.api.v1.sigma import router as sigma_router
from dfe_engine.api.v1.sources import router as sources_router
from dfe_engine.api.v1.synthetic_data import router as synthetic_data_router
from dfe_engine.api.v1.system import router as system_router
from dfe_engine.api.v1.tasks import router as tasks_router
from dfe_engine.api.v1.transforms import router as transforms_router
from dfe_engine.clickhouse.attribution import tags_context
from dfe_engine.gitops.repo import read_scope


async def _deploy_repo_scope():
    """One view of the deploy repo per request.

    Each engine replica reads its own clone, so the first read of the request takes
    the remote's head and every read after it answers from that same view; the next
    request checks again. Without the scope a listing would ask the deploy repo once
    per source.
    """
    with read_scope():
        yield


async def _attribution_scope(request: Request):
    """Scope CH query attribution to the request (feature = the route path).

    Every ClickHouse query the request issues is stamped with this feature in its
    ``log_comment`` (via the ClickHouseClientWrapper hook + ``current_tags``), so
    ``system.query_log`` and the query_log_archive cost leaderboard attribute cost
    per endpoint. Auth-agnostic (no user dependency) so it runs on public routes
    too; user/tenant enrichment is a follow-up needing the per-route auth context.
    """
    with tags_context(feature=request.url.path):
        yield


v1_router = APIRouter(
    prefix="/v1", dependencies=[Depends(_attribution_scope), Depends(_deploy_repo_scope)]
)
v1_router.include_router(auth_router)
# OIDC RP login/callback - self-prefixed /auth/oidc, unauthenticated (it IS login)
v1_router.include_router(oidc_login_router)

# Auth sub-routers mounted under /auth prefix
_auth_sub = APIRouter(prefix="/auth")
_auth_sub.include_router(accounts_router)
_auth_sub.include_router(account_groups_router)
_auth_sub.include_router(api_keys_router)
_auth_sub.include_router(roles_router)
_auth_sub.include_router(oidc_providers_router)
v1_router.include_router(_auth_sub)

v1_router.include_router(orgs_router)
v1_router.include_router(sources_router)
# The Kafka topics those sources imply: ensure, converge, status, remove
v1_router.include_router(kafka_topics_router)
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
v1_router.include_router(sampler_router)
v1_router.include_router(synthetic_data_router)
v1_router.include_router(pipeline_router)
v1_router.include_router(tasks_router)
v1_router.include_router(discovery_router)
# Ahead of schemas_router: /definitions/derived/... would otherwise be swallowed
# by its /definitions/{schema_path:path} routes.
v1_router.include_router(derived_schemas_router)
v1_router.include_router(schemas_router)
# SCIM 2.0 provisioning face (/scim/v2) over the account/group stores
v1_router.include_router(scim_router)
v1_router.include_router(sigma_router)
v1_router.include_router(cel_router)
# Governed Ops (Tier-1 helm-var CRUD + Tier-2 actions/admin) - 503 until gitops on
v1_router.include_router(helm_router)
# The substrate/platform half of the same overlay layer -- declared config out,
# governed writes in, storage model and modes locked by policy
v1_router.include_router(backing_services_router)
v1_router.include_router(apps_router)
# What each app image says its own config is, emitted by the pinned binary itself
v1_router.include_router(app_contracts_router)
# The versioned artefact library an app instance links its consumed files to
v1_router.include_router(library_router)
v1_router.include_router(governance_router)
v1_router.include_router(gitops_router)
v1_router.include_router(lifecycle_router)
v1_router.include_router(authoring_router)
# Runtime client-config bootstrap for the UI (public, no secrets)
v1_router.include_router(config_router)
# Per-org ClickHouse connection material the HyperDX fork seeds a team with
v1_router.include_router(hyperdx_router)
v1_router.include_router(repository_router)

__all__ = ["v1_router"]
