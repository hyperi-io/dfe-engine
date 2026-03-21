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
from hyperi_pylib.logger import logger

from dfe_engine.settings import DFESettings, load_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: bootstrap registries on startup, cleanup on shutdown."""
    settings: DFESettings = app.state.settings

    from dfe_engine.api.deps import bootstrap_registries, shutdown_registries

    bootstrap_registries(settings)
    logger.info(f"DFE Engine API started (port={settings.api.port})")
    yield
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

    # Health check (unauthenticated, outside /api/v1)
    @app.get("/health", tags=["System"], include_in_schema=False)
    async def health():
        return {"status": "healthy", "version": _get_version()}

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

    app.openapi = custom_openapi  # type: ignore[method-assign]

    return app


def _get_version() -> str:
    """Get package version, fallback to 'dev'."""
    try:
        from importlib.metadata import version

        return version("dfe-engine")
    except Exception:
        return "dev"
