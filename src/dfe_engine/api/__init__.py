"""DFE Engine API — FastAPI application.

Usage::

    from dfe_engine.api import create_app
    app = create_app()

Or via the daemon entry point (uses scalo DfeApp framework)::

    dfe-engine run          # start the API server
    dfe-engine version      # show version
    dfe-engine config-check # validate settings

``dfe-engine`` is the pure daemon dfe-infra runs in a k8s pod. It carries
only the scalo base subcommands (run / version / config-check). The
operator-facing offline commands (auth, gitops, governed-ops, sampler,
ch-cloud) live on the separate ``dfe`` CLI, not here.
"""

from __future__ import annotations

from dfe_engine.api.app import create_app


class _DfeEngineApp:
    """Entry point adapter using scalo DfeApp framework."""

    name = "dfe-engine"
    # env_prefix stays DFE_API: the API sub-config env vars (DFE_API_HOST,
    # DFE_API_PORT, DFE_API_JWT_SECRET, ...) and the Helm chart both key off
    # it. Renaming it would break settings resolution.
    env_prefix = "DFE_API"

    def _make_app(self):
        from scalo.cli import DfeApp, VersionInfo

        class DfeEngineApp(DfeApp):
            name = "dfe-engine"
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
                # Pure daemon: no offline command registrations. The
                # operator-facing commands (auth/gitops/governed-ops/
                # sampler/ch-cloud) live on the separate `dfe` CLI. The
                # daemon keeps only scalo's base run/version/config-check.
                pass

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

        return DfeEngineApp()


def run_dev_server() -> None:
    """Entry point for the ``dfe-engine`` console script.

    Delegates to scalo DfeApp CLI framework which provides:
    ``run``, ``version``, and ``config-check`` subcommands plus
    common flags (--config, --log-level, --verbose, --quiet).
    """
    _DfeEngineApp()._make_app().cli()


__all__ = ["create_app", "run_dev_server"]
