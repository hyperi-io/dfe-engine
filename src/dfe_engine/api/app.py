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
from fastapi.routing import APIRoute
from scalo.health import HealthManager, create_health_router
from scalo.logger import logger

from dfe_engine.settings import DFESettings, load_settings


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
    default_admin_pw = os.environ.get("DFE_ADMIN_PASSWORD", "changeme")
    account_store, group_store, api_key_store, role_store, role_config = bootstrap_auth(
        auth_dir, default_admin_password=default_admin_pw
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
        from dfe_engine.hyperdx.client import HyperDXClient

        api_key = os.environ.get(settings.hyperdx.api_key_env, "")
        app.state.hyperdx_client = HyperDXClient(
            base_url=settings.hyperdx.base_url,
            api_key=api_key,
        )
        logger.info("HyperDX client initialized", base_url=settings.hyperdx.base_url)

    # Org ClickHouse RBAC reconcile (opt-in via DFE_ORG_PROVISIONING_ENABLED).
    # Reconciles the seeded quota tiers + service roles + per-org roles/row
    # policies on _org_id into ClickHouse. Default-off so startup is unaffected;
    # fully non-fatal. Group bindings + user secret-minting are a follow-on
    # (reconcile via the CLI / governance API with a secrets store configured).
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
            # The secrets store mints the loader / query_reader service users;
            # without it only tiers, roles and org row policies reconcile.
            reconcile_ch_rbac(
                admin_client,
                secrets_store=build_secrets(settings.secrets),
                orgs=app.state.org_registry.list(),
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
        org_registry=getattr(app.state, "org_registry", None),
    )

    # Bootstrap task manager for async background tasks
    from dfe_engine.api.task_manager import TaskManager

    app.state.task_manager = TaskManager()

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

    app = FastAPI(
        title="DFE Engine API",
        description="Data Fusion Engine — configuration, scheduling, and query API",
        version=_get_version(),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
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
    app.include_router(create_health_router(health_manager))

    # Custom OpenAPI schema with Bearer auth
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        # scalo health router uses `-> JSONResponse` with
        # `from __future__ import annotations`, which breaks Pydantic OpenAPI
        # generation (unresolved ForwardRef). Strip response models for
        # /health/* only while building the schema (see openapi-spec/generate.py).
        saved_health: list[tuple[APIRoute, object, object]] = []
        for route in app.routes:
            if isinstance(route, APIRoute) and route.path.startswith("/health/"):
                saved_health.append((route, route.response_model, route.response_field))
                route.response_model = None
                route.response_field = None
        try:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
            )
        finally:
            for route, response_model, response_field in saved_health:
                route.response_model = response_model
                route.response_field = response_field
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

    app.openapi = custom_openapi  # type: ignore[method-assign]

    return app


def _get_version() -> str:
    """Get package version, fallback to 'dev'."""
    try:
        from importlib.metadata import version

        return version("dfe-engine")
    except Exception:
        return "dev"
