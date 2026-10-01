"""DFE Engine API -- FastAPI application.

Usage::

    from dfe_engine.api import create_app
    app = create_app()

Or via the daemon entry point (uses scalo ServiceApp framework)::

    dfe-engine run          # start the API server
    dfe-engine version      # show version
    dfe-engine config-check # validate settings

``dfe-engine`` is the pure daemon dfe-infra runs in a k8s pod. It carries
only the scalo base subcommands (run / version / config-check). The
operator-facing offline commands (auth, gitops, governed-ops, sampler,
ch-cloud) live on the separate ``dfe`` CLI, not here.

The other console scripts on the engine image are plain Typer apps, so they call
:func:`init_scalo_logger` to get the logger ServiceApp gives the daemon.
"""

import os

from scalo.cli import CommonArgs

from dfe_engine.api.app import create_app

# scalo reads its knobs and cascade keys as DFE_API_<KEY>, so every process on the image uses this.
ENV_PREFIX = "DFE_API"


def init_scalo_logger(service_name: str, *, otel_tracing: bool) -> None:
    """Install scalo's logger sinks for a console script that does not run on ServiceApp.

    The ``dfe-engine`` daemon gets this from ServiceApp's ``run``. A plain Typer app
    does not, so without this call every line goes through loguru's bare default
    handler: no scalo format, no secret scrubbing, no keyword fields. ``LOG_LEVEL``
    and ``LOG_FORMAT`` fill the slots the daemon's flags read them into, and the
    config cascade loads under the daemon's prefix.

    Args:
        service_name: ``service.name`` on exported spans.
        otel_tracing: Compose OTLP span export in. False for a one-shot command,
            which exits before an export interval elapses.

    Raises:
        scalo.cli.error.ConfigError: If the config cascade cannot be loaded.
        scalo.cli.error.LoggerError: If the logger cannot be initialised.
    """
    args = CommonArgs(
        log_level=os.environ.get("LOG_LEVEL"),
        log_format=os.environ.get("LOG_FORMAT"),
    )
    config = args.load_config(ENV_PREFIX)
    args.init_logger(config=config, service_name=service_name, otel_tracing=otel_tracing)


class _DfeEngineApp:
    """Entry point adapter using scalo ServiceApp framework."""

    def _make_app(self):
        from scalo.cli import ServiceApp, VersionInfo

        class DfeEngineApp(ServiceApp):
            name = "dfe-engine"
            env_prefix = ENV_PREFIX

            def version_info(self) -> VersionInfo:
                from dfe_engine import __version__

                return VersionInfo(self.name, __version__)

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
                # Share ServiceApp's OWN HealthManager -- the instance scalo's
                # observability server serves on 9090 /readyz. Without this the
                # lifespan sets ready on a DIFFERENT manager and the probe reads
                # an always-unready one. Its metrics manager, the one served on
                # 9090 /metrics, carries the engine's own counters for the same
                # reason.
                app = create_app(
                    settings=settings,
                    health_manager=self.health(),
                    metrics_manager=self._metrics,
                )
                # proxy_headers off here: create_app resolves the forwarded client
                # and scheme from api.forwarded_allow_ips, so trust is decided once.
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host=settings.api.host,
                        port=settings.api.port,
                        log_level="info",
                        proxy_headers=False,
                    )
                )
                await server.serve()

        return DfeEngineApp()


def run_dev_server() -> None:
    """Entry point for the ``dfe-engine`` console script.

    Delegates to scalo ServiceApp CLI framework which provides:
    ``run``, ``version``, and ``config-check`` subcommands plus
    common flags (--config, --log-level, --verbose, --quiet).
    """
    _DfeEngineApp()._make_app().cli()


__all__ = ["ENV_PREFIX", "create_app", "init_scalo_logger", "run_dev_server"]
