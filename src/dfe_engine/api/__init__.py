"""DFE Engine API — FastAPI application.

Usage::

    from dfe_engine.api import create_app
    app = create_app()

Or via CLI (uses pylib DfeApp framework)::

    dfe-api run          # start the API server
    dfe-api version      # show version
    dfe-api config-check # validate settings
"""

from __future__ import annotations

from dfe_engine.api.app import create_app


class _DfeApiApp:
    """Entry point adapter using hyperi-pylib DfeApp framework."""

    name = "dfe-api"
    env_prefix = "DFE_API"

    def _make_app(self):
        from hyperi_pylib.cli import DfeApp, VersionInfo

        class DfeApiApp(DfeApp):
            name = "dfe-api"
            env_prefix = "DFE_API"

            def version_info(self) -> VersionInfo:
                try:
                    from importlib.metadata import version

                    v = version("dfe-engine")
                except Exception:
                    v = "dev"
                return VersionInfo(self.name, v)

            def deployment_contract(self):
                from dfe_engine.deployment_contract import engine_deployment_contract

                return engine_deployment_contract()

            def register_commands(self, app) -> None:
                from dfe_engine.cli import register_auth_commands

                register_auth_commands(app)

            def run_service(self, config) -> None:
                # Delegate to async
                pass

            async def run_service_async(self, config) -> None:
                import uvicorn

                from dfe_engine.settings import load_settings

                settings = load_settings()
                app = create_app(settings=settings)
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host=settings.api.host,
                        port=settings.api.port,
                        log_level="info",
                    )
                )
                await server.serve()

        return DfeApiApp()


def run_dev_server() -> None:
    """Entry point for ``dfe-api`` console script.

    Delegates to hyperi-pylib DfeApp CLI framework which provides:
    ``run``, ``version``, and ``config-check`` subcommands plus
    common flags (--config, --log-level, --verbose, --quiet).
    """
    _DfeApiApp()._make_app().cli()


__all__ = ["create_app", "run_dev_server"]
