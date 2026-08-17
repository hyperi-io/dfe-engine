"""DFE Engine API — FastAPI application factory.

Usage::

    from dfe_engine.api.app import create_app
    app = create_app()

Or via CLI::

    dfe-engine
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from scalo.health import HealthManager, create_health_router
from scalo.logger import logger

from dfe_engine.settings import DFESettings, is_dev_posture, load_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: bootstrap registries on startup, cleanup on shutdown."""
    import os
    from pathlib import Path

    settings: DFESettings = app.state.settings
    health: HealthManager = app.state.health_manager

    from dfe_engine.bootstrap import ensure_storage

    ensure_storage(settings=settings)

    from dfe_engine.api.deps import bootstrap_registries, shutdown_registries

    # Governed Ops engine (Tier-1/Tier-2 over the gitops deploy repo). Built
    # BEFORE the registries so the SourceRegistry can back onto the deploy
    # repo's config/sources. None when gitops is disabled -> the governance
    # routers return 503 (not_configured) and sources fall back to the
    # plain sources directory.
    from dfe_engine.gitcrud.factory import build_gitcrud

    try:
        gitcrud = build_gitcrud(settings.gitops)
    except Exception as exc:  # never let gitops setup break app startup
        logger.warning("Governed Ops gitcrud unavailable", error=str(exc))
        gitcrud = None
    app.state.gitcrud = gitcrud

    bootstrap_registries(settings, gitcrud=gitcrud)

    # SUPPORT-DRIFT: name every component the deploy repo's pins.yaml moves
    # off the certified stack (untested combination, operator-owned risk).
    if gitcrud is not None:
        from dfe_engine.gitops.support_drift import log_support_drift

        try:
            log_support_drift(gitcrud.repo_path)
        except Exception as exc:  # the notice must never break startup
            logger.warning("SUPPORT-DRIFT check unavailable", error=str(exc))

    from dfe_engine.clickhouse.bootstrap import bootstrap_clickhouse

    bootstrap_clickhouse(settings=settings)

    # Bootstrap auth stores
    from dfe_engine.auth.bootstrap import bootstrap_auth
    from dfe_engine.auth.local_provider import LocalAuthProvider

    auth_dir_str = settings.auth.auth_dir
    if not auth_dir_str:
        config_dir = settings.config_dir or os.environ.get("DFE_CONFIG_DIR", "")
        if config_dir:
            auth_dir_str = str(Path(config_dir) / "auth")
        else:
            # Fallback to a temp-safe default under cwd
            auth_dir_str = str(Path("config") / "auth")

    auth_dir = Path(auth_dir_str)
    # Prefer the documented DFE_AUTH_LOCAL_ADMIN_PASSWORD (settings.auth.local),
    # then the legacy DFE_ADMIN_PASSWORD env, then the default. Without the first
    # source the documented + chart-used var was silently ignored, leaving the
    # admin on the well-known 'changeme' even when an operator set it.
    default_admin_pw = (
        settings.auth.local.admin_password or os.environ.get("DFE_ADMIN_PASSWORD") or "changeme"
    )
    account_store, group_store, api_key_store, role_store, role_config = bootstrap_auth(
        auth_dir, default_admin_password=default_admin_pw
    )
    app.state.account_store = account_store
    app.state.group_store = group_store
    app.state.api_key_store = api_key_store
    app.state.role_store = role_store
    app.state.role_config = role_config
    app.state.auth_provider = LocalAuthProvider(account_store, group_store)

    # JWT authority: the engine as the single ES384 issuer - signs, verifies, and
    # publishes the JWKS. Shares its scalo.secrets signing key with create_access_token.
    from dfe_engine.api.deps import jwt_authority_for

    app.state.jwt_authority = jwt_authority_for(settings)

    from dfe_engine.governance import PolicyStore

    # Forge client for opening review PRs when a production+team write may not
    # commit straight to main (gitcrud/routing.py). None -> that posture refuses.
    try:
        from dfe_engine.gitcrud.forge import build_forge

        app.state.forge = build_forge(settings.gitops) if gitcrud is not None else None
    except Exception as exc:  # never let forge setup break app startup
        logger.warning("gitops review-PR forge unavailable", error=str(exc))
        app.state.forge = None
    if gitcrud is not None:
        from dfe_engine.gitcrud.auto_merge import resolve_state, startup_banner

        startup_banner(resolve_state(gitcrud, environment=settings.env, mode=settings.gitops.mode))
    app.state.policy_store = PolicyStore(gitcrud) if gitcrud is not None else None

    # Bootstrap OIDC provider registry
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry

    oidc_dir = auth_dir / "oidc-providers"
    oidc_dir.mkdir(parents=True, exist_ok=True)
    app.state.oidc_provider_registry = OIDCProviderRegistry(oidc_dir)

    # Build the OIDC relying party from the enabled providers. The engine is the
    # RP + single token issuer: it terminates the IdP login and re-mints its own
    # ES384 token. Zero providers is fine (an empty registry -> every login 404s).
    from dfe_engine.auth.oidc.rp import OidcRelyingParty

    try:
        app.state.oidc_rp = OidcRelyingParty(app.state.oidc_provider_registry)
    except Exception as exc:  # never let RP setup break app startup
        logger.warning("OIDC relying party unavailable", error=str(exc))
        app.state.oidc_rp = None

    # Bootstrap connection registry for multi-tenant ClickHouse
    from dfe_engine.connections.config import ConnectionConfigLoader
    from dfe_engine.connections.registry import ConnectionRegistry

    conn_config_path = (
        Path(settings.config_dir or os.environ.get("DFE_CONFIG_DIR", ""))
        / "rbac"
        / "connections.yaml"
    )
    if conn_config_path.exists():
        conn_config = ConnectionConfigLoader.load(conn_config_path)
        logger.info(f"Loaded connection config from {conn_config_path}")
    else:
        conn_config = ConnectionConfigLoader.load_default()
    app.state.connection_registry = ConnectionRegistry(conn_config)

    # Bootstrap org registry
    from dfe_engine.orgs.registry import OrgRegistry

    orgs_dir_str = ""
    config_dir = settings.config_dir or os.environ.get("DFE_CONFIG_DIR", "")
    if config_dir:
        orgs_dir_str = str(Path(config_dir) / "orgs")
    else:
        orgs_dir_str = str(Path("config") / "orgs")
    app.state.org_registry = OrgRegistry(Path(orgs_dir_str))

    # Bootstrap service surface registry (schema-less Rust service discovery)
    from dfe_engine.services.surfaces.registry import SurfaceRegistry

    surfaces_config_dir = settings.config_dir or os.environ.get("DFE_CONFIG_DIR", "")
    if surfaces_config_dir:
        surfaces_dir = Path(surfaces_config_dir) / "service-surfaces"
    else:
        surfaces_dir = Path("config") / "service-surfaces"
    app.state.surface_registry = SurfaceRegistry(surfaces_dir)

    # Bootstrap HyperDX client (optional)
    if settings.hyperdx.enabled and settings.hyperdx.base_url:
        from dfe_engine.auth.jwt_authority import HYPERDX_AUDIENCE, MachineTokenSource
        from dfe_engine.hyperdx.client import HyperDXClient

        # Engine-signed machine JWTs replace the stored api key (dfe-engine#149).
        token_source = MachineTokenSource(app.state.jwt_authority, audience=HYPERDX_AUDIENCE)
        app.state.hyperdx_client = HyperDXClient(
            base_url=settings.hyperdx.base_url,
            token_provider=token_source.token,
        )
        logger.info("HyperDX client initialized", base_url=settings.hyperdx.base_url)

    # Org ClickHouse RBAC reconcile (opt-in via DFE_ORG_PROVISIONING_ENABLED).
    # Reconciles the seeded quota tiers + service roles + per-org roles/row
    # policies on _org_id into ClickHouse, plus one CH user per RBAC group
    # holding that group's org role. Default-off so startup is unaffected;
    # fully non-fatal.
    if os.environ.get("DFE_ORG_PROVISIONING_ENABLED", "").lower() in ("true", "1", "yes"):
        try:
            from dfe_engine.governance.ch import ch_admin_client, reconcile_from_stores

            reconcile_from_stores(
                ch_admin_client(settings),
                settings=settings,
                org_registry=app.state.org_registry,
                group_store=group_store,
            )
            logger.info("CH RBAC reconcile complete")
        except Exception:
            logger.exception("CH RBAC reconcile failed; continuing without it")

    # Bootstrap org lifecycle manager
    from dfe_engine.orgs.lifecycle import OrgLifecycleManager

    hdx_client = getattr(app.state, "hyperdx_client", None)
    app.state.org_lifecycle = OrgLifecycleManager(
        registry=app.state.org_registry,
        hyperdx_client=hdx_client,
        connection_config=conn_config,
    )

    # Bootstrap JIT provisioner
    from dfe_engine.auth.jit import JitProvisioner

    app.state.jit_provisioner = JitProvisioner(
        account_store=account_store,
        group_store=group_store,
        hyperdx_client=getattr(app.state, "hyperdx_client", None),
    )

    # Bootstrap task manager for async background tasks
    from dfe_engine.api.task_manager import TaskManager

    app.state.task_manager = TaskManager()

    # Sampler singleton (holds the shared logreducer concurrency gate)
    from dfe_engine.sampling import Sampler

    app.state.sampler = Sampler(settings.sampler, settings.kafka, settings.clickhouse)

    # Readiness reflects ClickHouse reachability. The engine's core paths
    # (ingest, load, hunt, query) all need CH, so a pod that cannot reach it is
    # not ready to serve: /readyz goes NotReady and k8s pulls it from the
    # Service until CH recovers. Self-healing, and liveness is untouched - the
    # pod is never restarted, it stays up for introspection. The probe is a
    # single bounded ping that BYPASSES the resilience retry/auto-wake budget (a
    # kubelet poll must fail fast and must never wake a paused CH Cloud).
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

    ch_manager = ClickHouseManager.get_instance(
        {
            "ch_host": settings.clickhouse.host,
            "ch_port": settings.clickhouse.port,
            "ch_username": settings.clickhouse.username,
            "ch_password": settings.clickhouse.password,
            "ch_secure": settings.clickhouse.secure,
            "ch_verify": settings.clickhouse.verify,
            "ch_ca_cert": settings.clickhouse.ca_cert,
        }
    )
    health.register_ready_check("clickhouse", ch_manager.ping)

    health.set_started()
    health.set_ready()
    logger.info(f"DFE Engine API started (port={settings.api.port})")
    yield
    health.set_ready(False)
    shutdown_registries()
    logger.info("DFE Engine API stopped")


def create_app(
    settings: DFESettings | None = None,
    cors_origins: list[str] | None = None,
    health_manager: HealthManager | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: DFE settings. Defaults to ``load_settings()``.
        cors_origins: CORS allowed origins. Overrides ``settings.api.cors_origins``.
        health_manager: the ``HealthManager`` the readiness state lives on. Under
            the ``dfe-engine`` daemon this is ServiceApp's OWN manager -- the same
            instance scalo's observability server serves on ``/readyz`` -- so the
            lifespan's ``set_ready`` and the probe agree. Left ``None`` (standalone
            ``create_app()`` / tests) a fresh manager is created.

    Returns:
        Configured FastAPI application.
    """
    settings = settings or load_settings()

    app = FastAPI(
        title="DFE Engine API",
        description="Data Fusion Engine — configuration, scheduling, and query API",
        version=_get_version(),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.state.settings = settings
    # Rebind the local so the health router below and the lifespan's set_ready
    # act on ONE manager. (Daemon: ServiceApp's own, served on 9090 /readyz.)
    health_manager = health_manager or HealthManager()
    app.state.health_manager = health_manager

    # Core-resource write guard (register before CORS so 409 responses still get CORS headers)
    from dfe_engine.api.middleware.core_resource_guard import install_core_resource_guard

    install_core_resource_guard(app)

    # CORS
    origins = cors_origins or settings.api.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Session cookie for the OIDC relying-party flow: Authlib's starlette client
    # stashes the OAuth state + nonce in request.session across the login/callback
    # redirect. Keyed by session_secret (>= jwt_secret's 32-byte floor when it
    # falls back). Not used by any other request path.
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.api.session_secret or settings.api.jwt_secret,
        same_site="lax",
        https_only=not is_dev_posture(settings.env),
    )

    # Exception handlers
    from dfe_engine.api.errors import install_exception_handlers

    install_exception_handlers(app)

    # API routes
    from dfe_engine.api.v1 import v1_router

    app.include_router(v1_router, prefix="/api")

    # JWKS + OIDC discovery (/.well-known/*) - public, so peers verify DFE tokens
    from dfe_engine.api.well_known import router as well_known_router

    app.include_router(well_known_router)

    # K8s health probes -- /livez and /readyz, the whole surface (no aliases)
    # include_in_schema=False: probes are not API surface, AND scalo's health
    # router returns `-> JSONResponse` (an unresolved ForwardRef under future
    # annotations) that breaks Pydantic OpenAPI generation. These routes nest
    # BELOW app.routes, so a path-based strip over app.routes silently matches
    # nothing (it did after the FastAPI/pydantic/scalo sweep) - excluding at the
    # include is the only reliable point. get_openapi recurses and would
    # otherwise pull in their JSONResponse stream_item_field.
    app.include_router(create_health_router(health_manager), include_in_schema=False)

    # /metrics is NOT mounted here (#106). It was unauthenticated on the
    # public API port; scalo's ServiceApp now serves it on the dedicated
    # observability port (9090), off the ingress-exposed 8000. The scrape and
    # the kubelet probes both target 9090 (see the chart). Health stays mounted
    # here too, on the SAME shared HealthManager, so 8000 and 9090 never
    # disagree -- but 9090 is the authoritative probe target.

    # Custom OpenAPI schema with Bearer auth
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "JWT Bearer token from /api/v1/auth/login",
            }
        }
        # Apply BearerAuth to all /api/ routes by default
        for path_key, path_item in schema.get("paths", {}).items():
            if path_key.startswith("/api/"):
                for method_data in path_item.values():
                    if isinstance(method_data, dict):
                        method_data.setdefault("security", [{"BearerAuth": []}])
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    return app


def _get_version() -> str:
    """Get package version, fallback to 'dev'."""
    try:
        from importlib.metadata import version

        return version("dfe-engine")
    except Exception:
        return "dev"
