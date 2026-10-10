#  Project:      dfe-engine
#  File:         bootstrap.py
#  Purpose:      The daemon's way into the schema bootstrap phase
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Run the schema phase on daemon startup, then reconcile the source tables' TTL.

The phase itself lives in :mod:`dfe_engine.schema.phase`, which applies the
pinned dfe-schemas manifest and is the only code path in the engine that issues
DDL for a declared object. This module is the daemon's way in and the place the
source tables are brought to the deployment default retention afterwards, so a
changed default reaches every deployed source's table on restart. The phase
refuses a changed default on a core table as TTL drift; an admin's change through
``PUT /api/v1/system/retention`` is what applies it there.

The phase is a GATE, not best-effort: its outcome decides readiness. Disable it
with ``DFE_CLICKHOUSE_BOOTSTRAP_TABLES=false``, which reports the schema state as
unknown and gates nothing. A pass ClickHouse failed for load or reachability runs
again in the background (:class:`SchemaRetry`), so one transient error does not hold
the pod NotReady until a restart.
"""

import math
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from scalo.logger import logger
from scalo.resilience import ReconnectingResilience, ResilienceConfig

from dfe_engine.backoff import jittered
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema.phase import STATE_FAILED, SchemaBootstrapState, run_bootstrap
from dfe_engine.settings import ClickHouseResilienceSettings, DFESettings, get_clickhouse_config

if TYPE_CHECKING:
    from dfe_engine.schema.metrics import SchemaMetrics
    from dfe_engine.source.models import Source


def _reconcile_sources(*, client: object, settings: DFESettings, sources: list[Source]) -> None:
    """Bring deployed source tables to the default TTL; a failure here never fails the phase."""
    from dfe_engine.schema.retention import reconcile_source_ttls

    try:
        outcome = reconcile_source_ttls(client, settings=settings, sources=sources)
    except Exception as exc:
        logger.error(f"source TTL reconcile failed: {exc}")
        return
    logger.info(f"bootstrap sources: {outcome.report.summary()}")
    if outcome.sources_skipped:
        logger.warning(
            "source TTL reconcile left sources to their next deploy",
            count=outcome.sources_skipped,
        )


def bootstrap_clickhouse(
    *,
    settings: DFESettings,
    sources: list[Source] | None = None,
    metrics: SchemaMetrics | None = None,
) -> SchemaBootstrapState:
    """Apply the manifest, then reconcile the deployed source tables' TTL.

    Returns the phase state. ``state.ready`` is what the readiness check reads,
    and ``state.converged`` is what says the objects exist -- a caller must not
    report a core table as deployed unless that one is true. ``metrics`` is where
    the phase reports its outcome; ``None`` reports nothing.
    """
    state = run_bootstrap(settings=settings, metrics=metrics)
    if not state.converged or not sources:
        return state

    try:
        manager = ClickHouseManager.get_instance(get_clickhouse_config(settings=settings))
        _reconcile_sources(
            client=manager.get_clickhouse_client(), settings=settings, sources=sources
        )
    except Exception as exc:  # the schema is applied; a TTL pass must not undo that
        logger.error(f"source TTL reconcile could not run: {exc}")
    return state


class _TransientPass(Exception):
    """A pass ClickHouse failed for load or reachability; it carries the pass's state."""

    def __init__(self, state: SchemaBootstrapState) -> None:
        super().__init__(state.error)
        self.state = state


class _RetryStopped(Exception):
    """The daemon is shutting down, so no further pass runs."""


class SchemaRetry:
    """Run the schema phase again, on a background thread, after a pass failed transiently.

    Passes are spaced on ``clickhouse.resilience``'s back-off steps, each jittered,
    with no budget: readiness stays false until a pass ends any other way, and the
    process never exits over it. A pass that converges calls ``on_converged``; one
    that fails for any other reason ends the retry and leaves the failure reported.

    Args:
        run_pass: One pass of the phase, returning its state.
        resilience: The back-off steps; ``enabled=false`` runs one more pass and stops.
        on_converged: Called once, on the retry's thread, after a pass converges.
    """

    def __init__(
        self,
        run_pass: Callable[[], SchemaBootstrapState],
        *,
        resilience: ClickHouseResilienceSettings,
        on_converged: Callable[[], None] | None = None,
    ) -> None:
        """Hold the pass and the back-off; nothing runs until :meth:`start` or :meth:`run`."""
        self._run_pass = run_pass
        self._resilience = resilience
        self._on_converged = on_converged
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="schema-bootstrap-retry", daemon=True
        )

    def start(self) -> None:
        """Start retrying in the background."""
        self._thread.start()

    def close(self, timeout: float) -> None:
        """Stop after the pass in flight, if any, waiting up to *timeout* seconds."""
        self._stop.set()
        self._thread.join(timeout)

    def run(self) -> SchemaBootstrapState | None:
        """Back off, then run passes until one ends other than transiently.

        Returns:
            The last pass's state, or None when :meth:`close` stopped the retry first.
        """

        def attempt() -> SchemaBootstrapState:
            state = self._run_pass()
            if state.state == STATE_FAILED and state.transient:
                raise _TransientPass(state)
            return state

        config = ResilienceConfig(
            **self._resilience.model_dump(exclude={"budget_seconds", "waking_budget_seconds"}),
            budget_seconds=math.inf,
            waking_budget_seconds=math.inf,
        )
        retrying = ReconnectingResilience(
            config,
            name="ClickHouse schema bootstrap",
            is_transient=lambda exc: isinstance(exc, _TransientPass),
            # Each pass connects afresh, so there is no pooled client to rebuild.
            is_reconnectable=lambda _exc: False,
            reconnect=lambda: None,
            sleep=self._sleep,
        )
        try:
            self._sleep(self._resilience.wait_initial)
            state = retrying.run(attempt)
        except _TransientPass as exc:
            return exc.state
        except _RetryStopped:
            return None
        if state.converged and self._on_converged is not None:
            self._on_converged()
        return state

    def _run(self) -> None:
        logger.warning("schema bootstrap failed transiently; running it again with back-off")
        state = self.run()
        if state is not None:
            logger.info("schema bootstrap retry finished", state=state.state)

    def _sleep(self, seconds: float) -> None:
        if self._stop.wait(jittered(seconds)):
            raise _RetryStopped
