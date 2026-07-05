"""DFE Engine API — FastAPI application factory.

Usage::

    from dfe_engine.api.app import create_app
    app = create_app()

Or via CLI::

    dfe-api
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

    from dfe_engine.api.deps import bootstrap_registries, shutdown_registries

    bootstrap_registries(settings)

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
    # None (not "changeme") when unset, so bootstrap_auth refuses to ship a usable
    # default admin password in a non-dev posture and generates one instead
    # (F-ADMIN-CHANGEME).
    admin_pw = os.environ.get("DFE_ADMIN_PASSWORD")
    account_store, group_store, api_key_store, role_store, role_config = bootstrap_auth(
        auth_dir,
        default_admin_password=admin_pw,
        dev_posture=is_dev_posture(settings.env),
    )
    app.state.account_store = account_store
    app.state.group_store = group_store
    app.state.api_key_store = api_key_store
    app.state.role_store = role_store
    app.state.role_config = role_config
    app.state.auth_provider = LocalAuthProvider(account_store, group_store)

    # Governed Ops engine (Tier-1/Tier-2 over the gitops deploy repo). None when
    # gitops is disabled -> the governance routers return 503 (not_configured).
    from dfe_engine.gitcrud.factory import build_gitcrud
    from dfe_engine.governance import PolicyStore

    try:
        gitcrud = build_gitcrud(settings.gitops)
    except Exception as exc:  # never let gitops setup break app startup
        logger.warning("Governed Ops gitcrud unavailable", error=str(exc))
        gitcrud = None
    app.state.gitcrud = gitcrud
    if gitcrud is not None:
        from dfe_engine.gitcrud.auto_merge import resolve_state, startup_banner

        startup_banner(resolve_state(gitcrud, environment=settings.env, mode=settings.gitops.mode))
    app.state.policy_store = PolicyStore(gitcrud) if gitcrud is not None else None

    # Bootstrap OIDC provider registry
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry

    oidc_dir = auth_dir / "oidc-providers"
    oidc_dir.mkdir(parents=True, exist_ok=True)
    app.state.oidc_provider_registry = OIDCProviderRegistry(oidc_dir)

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
        logger.info("Loaded connection config from %s", str(conn_config_path))
    else:
        conn_config = ConnectionConfigLoader.load_default()
    # Seed the registry's host/port from settings: all fixed CH users share ONE
    # cluster, so host/port come from settings (matching the admin manager the
    # direct-read endpoints used before) while the per-connection USER carries the
    # privilege. The shared conn_config (also handed to the org lifecycle) is left
    # untouched.
    app.state.connection_registry = ConnectionRegistry(
        conn_config,
        ch_host=settings.clickhouse.host,
        ch_port=settings.clickhouse.port,
    )

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
        from dfe_engine.hyperdx.client import HyperDXClient

        api_key = os.environ.get(settings.hyperdx.api_key_env, "")
        app.state.hyperdx_client = HyperDXClient(
            base_url=settings.hyperdx.base_url,
            api_key=api_key,
        )
        logger.info("HyperDX client initialized", base_url=settings.hyperdx.base_url)

    # ClickHouse RBAC reconcile (opt-in via DFE_ORG_PROVISIONING_ENABLED).
    # Reconciles the seeded quota tiers + service roles + fixed users by privilege
    # + ONE DFE_current_tenant_id row policy per _org_id table into ClickHouse.
    # Default-off so startup is unaffected; fully non-fatal. The service + fixed
    # user secrets are minted only when a secrets store is configured (below).
    if os.environ.get("DFE_ORG_PROVISIONING_ENABLED", "").lower() in ("true", "1", "yes"):
        try:
            from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
            from dfe_engine.governance.ch import reconcile_ch_rbac
            from dfe_engine.secrets import build_secrets

            ch_cfg = {
                "ch_host": settings.clickhouse.host,
                "ch_port": settings.clickhouse.port,
                "ch_username": settings.clickhouse.username,
                "ch_password": settings.clickhouse.password,
                "ch_secure": settings.clickhouse.secure,
                "ch_verify": settings.clickhouse.verify,
            }
            admin_client = ClickHouseManager.get_instance(ch_cfg).get_clickhouse_client()._client
            # The secrets store mints the service + fixed users; without it only
            # tiers, roles and the tenant row policies reconcile.
            reconcile_ch_rbac(
                admin_client,
                secrets_store=build_secrets(settings.secrets),
                orgs=app.state.org_registry.list(),
            )
            logger.info("CH RBAC reconcile complete")
        except Exception:
            logger.exception("CH RBAC reconcile failed; continuing without it")

    # Secrets seam (scalo.secrets) for the secrets the engine mints. Guarded: a
    # misconfigured backend must not break startup - the org lifecycle then mints
    # each HyperDX connection with an empty dfe_tenant_reader password (non-fatal,
    # filled on the next publish once the reconciler has minted the secret).
    from dfe_engine.secrets import build_secrets

    try:
        secrets_store = build_secrets(settings.secrets)
    except Exception as exc:
        logger.warning("Secrets seam unavailable", error=str(exc))
        secrets_store = None
    # Stash for request-time consumers (e.g. sigma providers resolving a git-token /
    # api-key secret path). None -> providers requiring a secret degrade with a clear
    # warning; no-auth providers (SigmaHQ public repo, Valhalla demo key) still work.
    app.state.dfe_secrets = secrets_store

    # Bootstrap org lifecycle manager. secrets_store feeds the per-org HyperDX
    # connection's dfe_tenant_reader password; per_group/ga_team_name select the
    # HyperDX team model (GA one-team vs per-org).
    from dfe_engine.orgs.lifecycle import OrgLifecycleManager

    hdx_client = getattr(app.state, "hyperdx_client", None)
    app.state.org_lifecycle = OrgLifecycleManager(
        registry=app.state.org_registry,
        hyperdx_client=hdx_client,
        connection_config=conn_config,
        secrets_store=secrets_store,
        per_group=settings.hyperdx.per_group,
        ga_team_name=settings.hyperdx.ga_team_name,
    )

    # Bootstrap JIT provisioner. role_config drives the HyperDX scope gate (Task C:
    # no HyperDX user for a principal whose effective access is none); per_group /
    # ga_team_name select the team model (Task D).
    from dfe_engine.auth.jit import JitProvisioner

    app.state.jit_provisioner = JitProvisioner(
        account_store=account_store,
        group_store=group_store,
        hyperdx_client=getattr(app.state, "hyperdx_client", None),
        org_registry=getattr(app.state, "org_registry", None),
        role_config=role_config,
        per_group=settings.hyperdx.per_group,
        ga_team_name=settings.hyperdx.ga_team_name,
    )

    # Bootstrap task manager for async background tasks
    from dfe_engine.api.task_manager import TaskManager

    app.state.task_manager = TaskManager()

    # Sampler singleton (holds the shared logreducer concurrency gate)
    from dfe_engine.sampling import Sampler

    app.state.sampler = Sampler(settings.sampler, settings.kafka, settings.clickhouse)

    # Query-views executor (parameterized ClickHouse views). Guarded: a CH
    # connection failure degrades /queries/views* to 503 (not_configured)
    # instead of blocking startup; auto_bootstrap applies the builtin views.
    from dfe_engine.query.bootstrap import build_view_executor

    app.state.view_executor = None
    if settings.clickhouse.host:
        try:
            app.state.view_executor = build_view_executor(
                settings, auto_bootstrap=settings.query_views.auto_bootstrap
            )
            logger.info("Query view executor ready")
        except Exception as exc:
            logger.warning("Query view executor unavailable", error=str(exc))

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
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: DFE settings. Defaults to ``load_settings()``.
        cors_origins: CORS allowed origins. Overrides ``settings.api.cors_origins``.

    Returns:
        Configured FastAPI application.
    """
    settings = settings or load_settings()

    # Swagger/ReDoc + the raw OpenAPI schema enumerate every route (incl.
    # gitops/governance/admin). Serve them ONLY in a dev posture; a non-dev deploy
    # returns 404 so the surface is not advertised to any client that reaches the
    # port (F-DOCS-EXPOSED). The app.openapi() METHOD stays callable for offline
    # spec generation - only the HTTP routes are gated.
    serve_docs = is_dev_posture(settings.env)
    app = FastAPI(
        title="DFE Engine API",
        description="Data Fusion Engine — configuration, scheduling, and query API",
        version=_get_version(),
        lifespan=lifespan,
        docs_url="/docs" if serve_docs else None,
        redoc_url="/redoc" if serve_docs else None,
        openapi_url="/openapi.json" if serve_docs else None,
    )

    app.state.settings = settings
    health_manager = HealthManager()
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

    # Exception handlers
    from dfe_engine.api.errors import install_exception_handlers

    install_exception_handlers(app)

    # API routes
    from dfe_engine.api.v1 import v1_router

    app.include_router(v1_router, prefix="/api")

    # K8s health probes — /health/live, /health/ready, /health/startup
    # include_in_schema=False: probes are not API surface, AND scalo's health
    # router returns `-> JSONResponse` (an unresolved ForwardRef under future
    # annotations) that breaks Pydantic OpenAPI generation. These routes nest
    # BELOW app.routes, so a path-based strip over app.routes silently matches
    # nothing (it did after the FastAPI/pydantic/scalo sweep) - excluding at the
    # include is the only reliable point. get_openapi recurses and would
    # otherwise pull in their JSONResponse stream_item_field.
    app.include_router(create_health_router(health_manager), include_in_schema=False)

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
