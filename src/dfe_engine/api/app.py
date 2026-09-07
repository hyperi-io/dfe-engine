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

from dfe_engine.settings import DFESettings, e2e_routes_enabled, is_dev_posture, load_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: bootstrap registries on startup, cleanup on shutdown."""
    import os
    from pathlib import Path

    settings: DFESettings = app.state.settings
    health: HealthManager = app.state.health_manager

    from dfe_engine.bootstrap import ensure_storage

    ensure_storage(settings=settings)

    # Fire-and-forget startup version check; on by default, any
    # version_check.* cascade key overrides (enabled: false kills it).
    # Must never break startup.
    try:
        from scalo.version_check import check_on_startup
        from scalo.version_check.checker import VersionCheckConfig

        from dfe_engine import __version__

        check_on_startup(
            product="dfe-engine",
            version=__version__,
            config=VersionCheckConfig.from_cascade_or(
                api_url="https://releases.hyperi.io/api/v1/check",
            ),
        )
    except Exception as exc:
        logger.debug("version check unavailable", error=str(exc))

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

    # Forge client for opening review PRs when a production+team write may not
    # commit straight to main (gitcrud/routing.py). None -> that posture refuses.
    # Ahead of the registries: the hunt and rule ones write through that path.
    try:
        from dfe_engine.gitcrud.forge import build_forge

        app.state.forge = build_forge(settings.gitops) if gitcrud is not None else None
    except Exception as exc:  # never let forge setup break app startup
        logger.warning("gitops review-PR forge unavailable", error=str(exc))
        app.state.forge = None

    bootstrap_registries(settings, gitcrud=gitcrud, forge=app.state.forge)

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

    # One central backend for local users AND groups: 'auto' (document store when a
    # reachable URI is configured, else yaml), 'document' (forced), or 'yaml'. The
    # document store is engine-wide and lifespan-owned so consumers share it.
    from dfe_engine.auth.store_backend import resolve_store_backend

    store_backend, doc_store = resolve_store_backend(
        settings.auth.store_backend,
        settings.auth.accounts_store.uri,
        settings.auth.accounts_store.database,
    )
    app.state.document_store = doc_store
    injected_account_store = None
    injected_group_store = None
    if store_backend == "document":
        from dfe_engine.auth.accounts import DocuStoreAccountStore
        from dfe_engine.auth.groups import DocuStoreGroupStore

        injected_account_store = DocuStoreAccountStore(
            doc_store, collection=settings.auth.accounts_store.collection
        )
        injected_group_store = DocuStoreGroupStore(doc_store)

    # Refuse to start on the shipped admin password outside a dev posture.
    from dfe_engine.auth.bootstrap import require_admin_password

    app.state.default_credentials = require_admin_password(
        settings.auth.local.admin_password, settings.env
    )

    account_store, group_store, api_key_store, role_store, role_config = bootstrap_auth(
        auth_dir,
        default_admin_password=settings.auth.local.admin_password,
        default_admin_name=settings.auth.local.admin_name,
        account_store=injected_account_store,
        group_store=injected_group_store,
        gitcrud=gitcrud,
        seed_accounts=settings.auth.local.seed_accounts,
        breakglass_password=settings.auth.local.breakglass_password,
    )
    app.state.account_store = account_store
    app.state.group_store = group_store
    app.state.api_key_store = api_key_store
    app.state.role_store = role_store
    app.state.role_config = role_config
    app.state.auth_provider = LocalAuthProvider(account_store, group_store)

    # Sensitive-attribute stores: a SEPARATE keyed store per entity kind, never
    # inline on the account/group model (a broad read would leak them). Backend
    # follows the account/group split - one document collection each in document
    # mode, else one YAML dir each under auth_dir.
    from dfe_engine.auth.attributes import AttributeStore, DocuStoreAttributeStore

    if store_backend == "document":
        app.state.account_sensitive_attributes = DocuStoreAttributeStore(
            doc_store, collection="sensitive_account_attributes"
        )
        app.state.group_sensitive_attributes = DocuStoreAttributeStore(
            doc_store, collection="sensitive_group_attributes"
        )
    else:
        app.state.account_sensitive_attributes = AttributeStore(
            auth_dir / "sensitive-account-attributes"
        )
        app.state.group_sensitive_attributes = AttributeStore(
            auth_dir / "sensitive-group-attributes"
        )

    # JWT authority: the engine as the single ES384 issuer - signs, verifies, and
    # publishes the JWKS. Shares its scalo.secrets signing key with create_access_token.
    from dfe_engine.api.deps import jwt_authority_for

    app.state.jwt_authority = jwt_authority_for(settings)

    from dfe_engine.governance import PolicyStore

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
    # This is only the INITIAL build - the RP is rebuilt whenever the provider
    # registry changes, so a new provider can serve logins without a restart.
    from dfe_engine.auth.oidc.rp import build_relying_party

    app.state.oidc_rp = build_relying_party(app.state.oidc_provider_registry)

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

    # Reconciles the seeded quota tiers + service roles + per-org roles/row
    # policies on _org_id into ClickHouse, plus one CH user per RBAC group
    # holding that group's org role. On by default: these policies ARE the tenant
    # fence, so a deployment that skipped them would carry _org_id on every row
    # and enforce none of it. Fully non-fatal.
    from dfe_engine.governance.ch import tenant_isolation_enabled

    if tenant_isolation_enabled():
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

    # Synthetic data service (synthetic reference streams; ceilings from settings)
    from dfe_engine.synthetic_data.autostart import start_autostart
    from dfe_engine.synthetic_data.service import SyntheticDataService

    app.state.synthetic_data = SyntheticDataService(settings.synthetic_data)
    # Standing demo streams - only when an operator configured autostart.
    app.state.synthetic_autostart = start_autostart(
        app.state.synthetic_data, settings.synthetic_data
    )

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
    autostart_tasks = getattr(app.state, "synthetic_autostart", [])
    for task in autostart_tasks:
        task.cancel()
    if autostart_tasks:
        # Await the cancellations so sink teardown (HTTP client close) runs.
        import asyncio

        await asyncio.gather(*autostart_tasks, return_exceptions=True)
    doc = getattr(app.state, "document_store", None)
    if doc:
        doc.close()
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
    e2e_docs = e2e_routes_enabled(settings)

    openapi_tags = None
    if e2e_docs:
        from dfe_engine.api.e2e import E2E_OPENAPI_TAG

        openapi_tags = [E2E_OPENAPI_TAG]
    app = FastAPI(
        title="DFE Engine API",
        description="Data Fusion Engine — configuration, scheduling, and query API",
        version=_get_version(),
        lifespan=lifespan,
        docs_url=None if e2e_docs else "/docs",
        redoc_url="/redoc",
        openapi_tags=openapi_tags,
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

    # Added last so it is the outermost middleware: every route below it reads the
    # scheme and client address the trusted gateway forwarded, which is what keeps
    # the OIDC redirect_uri on the https the caller arrived on. Lives in the app,
    # not the ASGI server, so one setting decides trust whatever serves it.
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=settings.api.forwarded_allow_ips,
    )

    # Exception handlers
    from dfe_engine.api.errors import install_exception_handlers

    install_exception_handlers(app)

    # API routes
    from dfe_engine.api.v1 import v1_router

    app.include_router(v1_router, prefix="/api")

    if e2e_docs:
        from dfe_engine.api.e2e import router as e2e_router

        app.include_router(e2e_router, prefix="/api")
        logger.warning("e2e-server routes mounted at /api/e2e")

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
            tags=app.openapi_tags,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "JWT Bearer token from /api/v1/auth/login",
            }
        }
        # Apply BearerAuth to all /api/ routes by default. The e2e-server
        # helpers are unauthenticated and must stay that way in Swagger.
        for path_key, path_item in schema.get("paths", {}).items():
            if path_key.startswith("/api/e2e"):
                continue
            if path_key.startswith("/api/"):
                for method_data in path_item.values():
                    if isinstance(method_data, dict):
                        method_data.setdefault("security", [{"BearerAuth": []}])
        if e2e_docs:
            from dfe_engine.api.e2e_docs import split_openapi

            api_schema, _ = split_openapi(schema)
            app.openapi_schema = api_schema
        else:
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    if e2e_docs:
        from dfe_engine.api.e2e_docs import install_e2e_swagger

        install_e2e_swagger(app)

    return app


def _get_version() -> str:
    """Get package version, fallback to 'dev'."""
    try:
        from importlib.metadata import version

        return version("dfe-engine")
    except Exception:
        return "dev"
