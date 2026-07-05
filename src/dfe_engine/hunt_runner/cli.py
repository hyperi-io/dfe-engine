#  Project:      dfe-engine
#  File:         hunt_runner/cli.py
#  Purpose:      dfe-hunt-runner CLI - wire the CH-coordinated runner + KEDA schedule
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""`dfe-hunt-runner` - the process entrypoints for the pull-based hunt runner.

This module is only WIRING. The moving parts are built and tested elsewhere: the
runner coordinates every decision through ClickHouse (ChCoordinator - lease,
watermark, state), the worker runs one windowed query, and the daemon is a thin
`while: tick(); sleep` loop. Keeping those pure means this CLI is the one place
that touches the wall clock, real signals, and a live ClickHouse client - so it
stays deliberately small.

Two commands, matching the two ways the runner is driven:

  materialise - the ONE-SHOT the Argo PostSync hook (or the engine, on a hunt-config
      change) runs to write the deterministic ``hunt_schedule`` table KEDA scales on.
      It is deploy-time, NOT the hot path - it runs once per config change, not per
      tick, so it just publishes and exits.

  run - the long-running worker. It builds the CH coordinator, ensures the
      coordination schema, then repeats ``HuntRunner.tick`` on a poll interval until
      k8s sends SIGTERM (or a local ctrl-c sends SIGINT). Add a pod and it competes
      for claims; kill one and its lease expires and is reclaimed - the coordination
      is entirely in ClickHouse, so the loop here owns nothing but cadence + signals.

The runner is held in a mutable one-element cell so ``on_reload`` can rebuild it
from freshly loaded specs WITHOUT the daemon loop knowing specs exist - the loop
stays agnostic (it only calls the tick and the reload callbacks it was handed).
"""

from __future__ import annotations

import signal
import time
from typing import Any

import clickhouse_connect
import typer

from dfe_engine.settings import DFESettings, load_settings

from .ch_coordinator import ChCoordinator
from .daemon import run_loop
from .runner import HuntRunner
from .schedule import publish_schedule
from .spec_loader import load_specs
from .worker import HuntWorker

app = typer.Typer(help="dfe-hunt-runner: CH-coordinated pull-based hunt execution.")


def _ch_params(settings: DFESettings) -> dict[str, Any]:
    """The clickhouse-connect kwargs for the raw client, derived from settings.

    Pure (opens no connection) so the settings -> params mapping is unit-testable
    without a live ClickHouse - the same host/port/username/password/secure/verify
    the integration ``ch_client`` fixture builds a client from.
    """
    ch = settings.clickhouse
    return {
        "host": ch.host,
        "port": ch.port,
        "username": ch.username,
        "password": ch.password,
        "secure": ch.secure,
        "verify": ch.verify,
    }


def _build_ch(settings: DFESettings) -> tuple[Any, str]:
    """Build a RAW clickhouse-connect client + resolve the data database name.

    Returns ``(client, database)``. The coordinator calls ``.insert()`` (which the
    ClickHouseClientWrapper does not expose), so this hands back the raw
    clickhouse-connect client, built from _ch_params (the SAME fields the integration
    ``ch_client`` fixture uses). The database is ``effective_data_database`` - NEVER a
    hardcoded 'dfe'; the coordination + schedule tables live in whatever data database
    the connection resolves to.
    """
    client = clickhouse_connect.get_client(**_ch_params(settings))
    return client, settings.clickhouse.effective_data_database


@app.command("materialise")
def materialise() -> None:
    """Publish the deterministic ``hunt_schedule`` table KEDA scales on (one-shot).

    Deploy-time, not the hot path: the Argo PostSync hook runs this once per
    hunt-config change so KEDA can answer "is any hunt due?" with zero workers
    running. Idempotent - re-materialising tombstones removed hunts.
    """
    settings = load_settings()
    ch, db = _build_ch(settings)
    live = publish_schedule(ch, db, load_specs(settings.hunts.hunt_dir))
    typer.echo(f"materialised {live} hunt(s) into {db}.hunt_schedule")


@app.command("run")
def run(
    poll: float = typer.Option(15.0, help="Seconds between runner ticks."),
    cap: int = typer.Option(8, help="Global cap on concurrent hunt runs (protects ClickHouse)."),
    reload_every: int = typer.Option(
        20, help="Reload hunt specs (and rebuild the runner) every N ticks (0 = never)."
    ),
    worker_id: str = typer.Option(
        "",
        "--worker-id",
        help=(
            "Lease-owner id for this worker. Default derives hostname-pid, unique "
            "per pod - claimants sharing an id would double-run."
        ),
    ),
) -> None:
    """Run the long-lived pull-based worker until SIGTERM/SIGINT.

    Repeats one runner tick (due -> claim -> execute, all via ClickHouse) on the
    poll interval. Every ``reload_every`` ticks it reloads hunt specs from gitops
    and rebuilds the runner in place, so a hunt added/removed in git is picked up
    without a restart. A stop flag flipped by SIGTERM/SIGINT drains promptly (the
    daemon re-checks it right after each tick, before sleeping).
    """
    settings = load_settings()
    ch, db = _build_ch(settings)
    coord = ChCoordinator(ch, database=db, worker_id=worker_id or None)
    coord.ensure_schema()
    worker = HuntWorker(ch, coord)
    hunt_dir = settings.hunts.hunt_dir

    def _build_runner() -> HuntRunner:
        return HuntRunner(coord, worker, load_specs(hunt_dir), cap=cap)

    # The runner lives in a one-element cell so on_reload can swap in a runner built
    # from freshly loaded specs. The daemon loop only ever calls cell[0].tick, so it
    # never needs to know specs (or a rebuild) exist.
    cell: list[HuntRunner] = [_build_runner()]

    # SIGTERM (k8s pod stop) and SIGINT (local ctrl-c) both flip one stop flag; the
    # daemon owns the drain (checks the flag after each tick and before sleeping).
    stop = {"flag": False}

    def _request_stop(_signum: int, _frame: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    def _reload() -> None:
        cell[0] = _build_runner()

    ticks = run_loop(
        tick=lambda now: cell[0].tick(now),
        should_stop=lambda: stop["flag"],
        clock=time.time,
        sleep=time.sleep,
        poll_seconds=poll,
        on_reload=_reload,
        reload_every=reload_every,
    )
    typer.echo(f"hunt-runner stopped after {ticks} tick(s)")


def main() -> None:
    """Console-script entrypoint (``dfe-hunt-runner``)."""
    app()
