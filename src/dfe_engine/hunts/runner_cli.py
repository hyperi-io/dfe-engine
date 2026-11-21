#  Project:      dfe-engine
#  File:         hunts/runner_cli.py
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
that touches actual time, real signals, and a live ClickHouse client - so it
stays deliberately small.

Two commands, matching the two ways the runner is driven:

  materialise - the ONE-SHOT run to write the deterministic ``hunt_schedule`` table
      KEDA scales on.
      It is NOT the hot path - it publishes and exits. The engine API publishes that
      table after each hunt change and a running worker keeps it current too, so this
      is for a hunt committed to the deploy repo by hand while no worker is running.

  run - the long-running worker. It builds the CH coordinator, ensures the
      coordination schema, then repeats ``HuntRunner.tick`` on a poll interval until
      k8s sends SIGTERM (or a local ctrl-c sends SIGINT). A ClickHouse that is down,
      or does not know the runner's user yet, is waited out at the connect and at
      every tick rather than ending the process (see ``connect``). It publishes
      ``hunt_schedule`` when it starts and after every spec reload, so the table KEDA
      scales on follows the hunt config. Add a pod and it competes
      for claims. Kill one and its lease expires and is reclaimed - the coordination
      is entirely in ClickHouse, so the loop here owns nothing but cadence + signals.

The runner is held in a ``_ReloadingRunner`` so ``on_reload`` can rebuild it from
freshly loaded specs WITHOUT the daemon loop knowing specs exist - the loop stays
agnostic (it only calls the tick and the reload callbacks it was handed).
"""

import signal
import socket
import time
from collections.abc import Callable, Mapping
from typing import Any

import clickhouse_connect
import typer
from scalo.logger import logger

from dfe_engine.api import init_scalo_logger
from dfe_engine.clickhouse.tls import resolve_clickhouse_tls
from dfe_engine.hunts import runner_metrics
from dfe_engine.hunts.alert import AlertDestinationRegistry
from dfe_engine.hunts.ch_coordinator import ChCoordinator
from dfe_engine.hunts.connect import TickGuard, connect_when_available, unavailable_reason
from dfe_engine.hunts.daemon import run_loop
from dfe_engine.hunts.delivery import MatchNotifier
from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.hunts.lookups import refuse_tenant_lookups
from dfe_engine.hunts.runner import HuntFailures, HuntRunner
from dfe_engine.hunts.schedule import restore_hash_offsets
from dfe_engine.hunts.schedule_publish import publish_hunt_schedule, spec_sources
from dfe_engine.hunts.smoothing import PhaseSmoother
from dfe_engine.hunts.spec_loader import load_specs
from dfe_engine.hunts.worker import HuntWorker
from dfe_engine.schema.plan import LOCK_ID, object_names
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import DFESettings, load_settings

app = typer.Typer(help="dfe-hunt-runner: CH-coordinated pull-based hunt execution.")


def _ch_params(settings: DFESettings) -> dict[str, Any]:
    """The clickhouse-connect kwargs for the raw client, derived from settings.

    Pure (opens no connection) so the settings -> params mapping is unit-testable
    without a live ClickHouse - the same host/port/username/password/secure/verify/
    ca_cert the integration ``ch_client`` fixture builds a client from.
    """
    ch = settings.clickhouse
    tls = resolve_clickhouse_tls(secure=ch.secure, verify=ch.verify, ca_cert=ch.ca_cert)
    return {
        "host": ch.host,
        "port": ch.port,
        "username": ch.username,
        "password": ch.password,
        **tls.connect_kwargs(),
    }


def _build_ch(settings: DFESettings) -> tuple[Any, str]:
    """Build a RAW clickhouse-connect client + resolve the data database name.

    Returns ``(client, database)``. The coordinator calls ``.insert()`` (which the
    ClickHouseClientWrapper does not expose), so this hands back the raw
    clickhouse-connect client, built from _ch_params (the SAME fields the integration
    ``ch_client`` fixture uses). The database is ``effective_data_database`` - NEVER a
    hardcoded 'dfe'; the coordination + schedule tables live in whatever data database
    the connection resolves to.

    The client opens no ClickHouse session, because the lease renewals run on their
    own thread beside a hunt's statement and a session takes one query at a time.
    """
    client = clickhouse_connect.get_client(**_ch_params(settings), autogenerate_session_id=False)
    return client, settings.clickhouse.effective_data_database


def _connect_coordinator(settings: DFESettings) -> tuple[Any, ChCoordinator]:
    """One attempt at a raw client whose coordination tables are in place.

    A client from a failed attempt is closed before the error goes on, so a retry
    never leaves a pool behind. A lookup database holding a table that can carry an
    org's rows stops the runner here, before it fires anything.

    Raises:
        LookupDatabaseError: A listed lookup database holds such a table.
    """
    client, db = _build_ch(settings)
    try:
        # The hostname is the pod name under k8s: stable per process, distinct per pod.
        coord = ChCoordinator(client, database=db, worker_id=socket.gethostname())
        coord.ensure_schema()
        refuse_tenant_lookups(client, settings.hunts.lookup_databases)
    except Exception:
        client.close()
        raise
    return client, coord


class _ReloadingRunner:
    """The current runner, rebuilt on each reload, whose next tick rechecks the lookups first.

    The recheck runs inside the tick, so the tick guard skips a tick ClickHouse cannot
    answer and the recheck waits for the next one, firing nothing in between. A table
    that turned unsafe ends the process, as it does at the connect.

    Args:
        build: builds a runner from freshly loaded specs.
        check: raises :class:`LookupDatabaseError` while a lookup database is unsafe.
    """

    def __init__(self, build: Callable[[], HuntRunner], check: Callable[[], None]) -> None:
        self._build = build
        self._check = check
        self._runner = build()
        self._unchecked = False

    def tick(self, now: int) -> int:
        """Recheck the lookups if a reload left them unchecked, then tick the runner."""
        if self._unchecked:
            self._check()
            self._unchecked = False
        return self._runner.tick(now)

    def reload(self) -> None:
        """Swap in a runner from freshly loaded specs, the lookups unchecked until the next tick."""
        self._runner = self._build()
        self._unchecked = True


def _match_notifier(settings: DFESettings) -> MatchNotifier:
    """The notifier over the destinations the engine API stores in the secrets backend.

    The runner reads each destination by name from the same ``settings.secrets``
    backend the engine writes, so it shares no volume with the engine.
    """
    registry = AlertDestinationRegistry(secrets=build_secrets(settings.secrets))
    return MatchNotifier(registry, global_channels=settings.hunts.alert_channels)


class _SchedulePublisher:
    """Publishes ``hunt_schedule`` for the specs it is handed, and owes the publish until it lands.

    A runner that cannot publish still runs its hunts, so a refused connection, a
    refused user or a server out of capacity leaves the publish owed and ``retry``
    tries it again on each tick until it gets through. Any other failure ends the
    process, as it does at the connect.

    Args:
        ch: the raw ClickHouse client.
        db: the data database holding the schedule.
        settings: the engine settings, for the offset fraction and the smoothing flag.
    """

    def __init__(self, ch: Any, db: str, settings: DFESettings) -> None:
        self._ch = ch
        self._db = db
        self._settings = settings
        self._owed: Mapping[str, HuntSpec] | None = None
        self._down = False

    def publish(self, specs: Mapping[str, HuntSpec]) -> None:
        """Publish *specs*, replacing any publish still owed."""
        self._owed = specs
        self.retry()

    def retry(self) -> None:
        """Publish what is owed, if anything is, leaving it owed while ClickHouse cannot answer."""
        if self._owed is None:
            return
        try:
            publish_hunt_schedule(self._ch, self._db, self._owed, self._settings)
        except Exception as exc:
            reason = unavailable_reason(exc)
            if reason is None:
                raise
            if not self._down:
                logger.warning(
                    "hunt_schedule not published, retrying on each tick",
                    reason=reason,
                    error=str(exc),
                )
            self._down = True
            return
        self._owed = None
        self._down = False


@app.command("materialise")
def materialise() -> None:
    """Publish the deterministic ``hunt_schedule`` table KEDA scales on (one-shot).

    The engine API does this after each hunt change and a running runner does it at
    start and on every reload, so this is for a hunt committed to the deploy repo by
    hand while no worker is running. Idempotent: re-materialising tombstones removed hunts.
    """
    init_scalo_logger(runner_metrics.SERVICE_NAME, otel_tracing=False)
    settings = load_settings()
    ch, db = _build_ch(settings)
    specs = load_specs(settings.hunts.hunt_dir, **spec_sources(settings, db))
    live = publish_hunt_schedule(ch, db, specs, settings)
    typer.echo(f"materialised {live} hunt(s) into {db}.hunt_schedule")


def _smoother(
    ch: Any, db: str, settings: DFESettings, obs: runner_metrics.HuntRunnerMetrics
) -> PhaseSmoother | None:
    """The phase smoother when ``hunts.smoothing.enabled``, else None after restoring the hash offsets.

    On, the runner says so at WARN and reports it as a gauge, since it is a beta that
    moves fire times. Off, any offset a past run learned is rewritten to the hash, so
    the runner and KEDA agree again, and a failed rewrite is logged rather than fatal.
    """
    smoothing = settings.hunts.smoothing
    fraction = settings.hunts.phase_spread_fraction
    obs.smoothing_enabled(smoothing.enabled)
    if not smoothing.enabled:
        try:
            restored = restore_hash_offsets(ch, db, fraction=fraction)
        except Exception as exc:
            logger.error("hunt_schedule offsets not restored to the hash", error=str(exc))
            return None
        if restored:
            logger.info("hunt_schedule offsets restored to the hash", hunts=restored)
        return None
    logger.warning(
        "hunt smoothing is on: a beta that drifts rate-hunt offsets to flatten ClickHouse load",
        cycle_seconds=smoothing.cycle_seconds,
        window_days=smoothing.window_days,
    )
    return PhaseSmoother(
        ch,
        db,
        lock_table=object_names(LOCK_ID, data_database=db)[LOCK_ID],
        settings=smoothing,
        fraction=fraction,
        metrics=obs,
    )


@app.command("run")
def run(
    poll: float | None = typer.Option(
        None, help="Seconds between runner ticks (default: hunts.runner_poll_seconds)."
    ),
    cap: int = typer.Option(8, help="Global cap on concurrent hunt runs (protects ClickHouse)."),
    reload_every: int = typer.Option(
        20, help="Reload hunt specs (and rebuild the runner) every N ticks (0 = never)."
    ),
) -> None:
    """Run the long-lived pull-based worker until SIGTERM/SIGINT.

    Repeats one runner tick (due -> claim -> execute, all via ClickHouse) on the
    poll interval. Every ``reload_every`` ticks it reloads hunt specs from gitops
    and rebuilds the runner in place, so a hunt added/removed in git is picked up
    without a restart, and rechecks the lookup databases before it fires again. A
    stop flag flipped by SIGTERM/SIGINT drains promptly (the daemon re-checks it
    right after each tick, before sleeping).

    The poll interval defaults to the setting rather than a literal, because the API
    quotes that setting back to whoever queues an ad-hoc run as the wait to expect.
    """
    init_scalo_logger(runner_metrics.SERVICE_NAME, otel_tracing=True)
    settings = load_settings()
    poll_seconds = settings.hunts.runner_poll_seconds if poll is None else poll
    # The long-running worker is the only entry point that reports telemetry. The
    # one-shot materialise publishes and exits before an export interval elapses.
    obs = runner_metrics.create()
    ch, coord = connect_when_available(
        lambda: _connect_coordinator(settings),
        resilience=settings.clickhouse.resilience,
        metrics=obs,
        user=settings.clickhouse.username,
    )
    db = settings.clickhouse.effective_data_database
    worker = HuntWorker(
        ch,
        coord,
        metrics=obs,
        notifier=_match_notifier(settings),
        overlap_seconds=settings.hunts.window_overlap_seconds,
        safe_horizon=settings.hunts.safe_horizon,
        max_deferral_seconds=settings.hunts.safe_horizon_max_deferral_seconds,
    )
    hunt_dir = settings.hunts.hunt_dir
    sources = spec_sources(settings, db)
    # Outlives each rebuild below, so a reload does not restart a failing hunt's back-off.
    failures = HuntFailures.from_settings(settings.hunts)
    # Outlives each rebuild too, so a reload does not reset the cycle clock or the lease.
    smoother = _smoother(ch, db, settings, obs)

    publisher = _SchedulePublisher(ch, db, settings)

    def _build_runner() -> HuntRunner:
        specs = load_specs(hunt_dir, **sources)
        publisher.publish(specs)
        return HuntRunner(
            coord,
            worker,
            specs,
            cap=cap,
            poll_seconds=poll_seconds,
            metrics=obs,
            failures=failures,
            phase_fraction=settings.hunts.phase_spread_fraction,
            smoother=smoother,
        )

    runners = _ReloadingRunner(
        _build_runner, lambda: refuse_tenant_lookups(ch, settings.hunts.lookup_databases)
    )

    # SIGTERM (k8s pod stop) and SIGINT (local ctrl-c) both flip one stop flag. The
    # daemon owns the drain (checks the flag after each tick and before sleeping).
    stop = {"flag": False}

    def _request_stop(_signum: int, _frame: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    guarded_tick = TickGuard(runners.tick, metrics=obs)

    def _tick(now: int) -> int:
        publisher.retry()
        return guarded_tick(now)

    ticks = run_loop(
        tick=_tick,
        should_stop=lambda: stop["flag"],
        clock=time.time,
        sleep=time.sleep,
        poll_seconds=poll_seconds,
        on_reload=runners.reload,
        reload_every=reload_every,
    )
    typer.echo(f"hunt-runner stopped after {ticks} tick(s)")


def main() -> None:
    """Console-script entrypoint (``dfe-hunt-runner``)."""
    app()
