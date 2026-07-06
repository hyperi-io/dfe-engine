#  Project:      dfe-engine
#  File:         cli/ch_cloud.py
#  Purpose:      `dfe-api ch-cloud` subcommands -- ClickHouse Cloud lifecycle
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""``dfe-api ch-cloud`` subcommands - ClickHouse Cloud service lifecycle.

See / start / stop the CH Cloud service directly over the management API (NO HTTP
hop to the engine), so this doubles as an ops tool AND a k8s initContainer wake
step - it works even when the engine is not running. Config comes from the
``clickhouse.cloud`` block (``DFE_CLICKHOUSE_CLOUD_*`` - the control-plane key).
A start is BILLABLE. Replaces the standalone scripts/ch_cloud.py helper.
"""

from __future__ import annotations

import typer
from scalo.cli import Typer
from scalo.cli.output import print_error, print_info

ch_cloud_app = Typer(help="ClickHouse Cloud service lifecycle (status/start/stop).")


def _service():
    from dfe_engine.clickhouse.cloud import CloudService
    from dfe_engine.settings import load_settings

    return CloudService(load_settings().clickhouse.cloud)


@ch_cloud_app.command("status")
def ch_cloud_status() -> None:
    """Show the CH Cloud service's control-plane state."""
    from dfe_engine.clickhouse.cloud import CloudServiceError

    try:
        st = _service().status()
    except CloudServiceError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_info(f"{st.name} ({st.id}): {st.state}")


@ch_cloud_app.command("start")
def ch_cloud_start(
    wait: bool = typer.Option(False, "--wait", help="Block until the service is running."),
    timeout: float = typer.Option(600.0, "--timeout", help="Wait budget in seconds with --wait."),
) -> None:
    """Start (wake) the CH Cloud service. BILLABLE + idempotent."""
    from dfe_engine.clickhouse.cloud import CloudServiceError

    svc = _service()
    try:
        st = svc.start()
        print_info(f"{st.name}: start issued -> {st.state}")
        if wait and not st.is_running:
            st = svc.wait_running(timeout=timeout)
            print_info(f"{st.name}: {st.state}")
    except CloudServiceError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc


@ch_cloud_app.command("stop")
def ch_cloud_stop() -> None:
    """Stop the CH Cloud service (saves cost). Idempotent."""
    from dfe_engine.clickhouse.cloud import CloudServiceError

    try:
        st = _service().stop()
    except CloudServiceError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    print_info(f"{st.name}: stop issued -> {st.state}")


def register_ch_cloud_commands(app) -> None:
    """Register the ``ch-cloud`` command group on the dfe-api CLI."""
    app.add_typer(ch_cloud_app, name="ch-cloud")
