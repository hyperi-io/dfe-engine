#  Project:      dfe-engine
#  File:         hunts/schedule_publish.py
#  Purpose:      Publish hunt_schedule from hunt specs, for the runner and the engine API
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Bring ``hunt_schedule`` to the stored hunts, from the runner and from the engine API.

KEDA scales the hunt runner on ``hunt_schedule``. A runner publishes it at start and
after every spec reload, but at ``minReplicaCount: 0`` no runner is running when a hunt
changes, so the engine API publishes it after every hunt change too. Both go through
:func:`publish_hunt_schedule`, so the rows are the same whichever wrote them.
"""

import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.hunts.hunt_target_check import DETECTION_TABLE
from dfe_engine.hunts.rule_compiler import detection_cap
from dfe_engine.hunts.schedule import publish_schedule
from dfe_engine.hunts.spec_loader import load_specs
from dfe_engine.settings import DFESettings


def spec_sources(
    settings: DFESettings, database: str, rules_dir: str | Path | None = None
) -> dict[str, Any]:
    """The loader's rule inputs: rule YAML, the default tables, the cap and the lookups.

    A hunt names `rules`, so the loader has to read them to build its SQL. The
    default target is the core detection table in the resolved data database - never
    a hardcoded 'dfe' - used only when neither the rule entry nor the hunt names one.
    A source named without a database is read from that same data database. Every
    compiled rule is capped at ``hunts.max_detections_per_run``, cut to its ceiling,
    and may look values up only in ``hunts.lookup_databases``.

    Args:
        settings: The engine settings.
        database: The data database.
        rules_dir: The rule directory when it is not ``hunts.rules_dir``, as under gitops.
    """
    hunts = settings.hunts
    return {
        "rules_dir": hunts.rules_dir if rules_dir is None else rules_dir,
        "default_target": f"{database}.{DETECTION_TABLE}",
        "default_database": database,
        "max_detections": detection_cap(
            hunts.max_detections_per_run, hunts.max_detections_per_run_ceiling
        ),
        "lookup_databases": tuple(hunts.lookup_databases),
    }


def publish_hunt_schedule(
    ch: Any, db: str, specs: Mapping[str, HuntSpec], settings: DFESettings
) -> int:
    """Bring ``hunt_schedule`` to *specs*, keeping an offset the smoother learned.

    With smoothing on, an offset learned for a hunt whose interval is unchanged is kept
    rather than reset to the hash. Returns the live hunt count.
    """
    return publish_schedule(
        ch,
        db,
        specs,
        fraction=settings.hunts.phase_spread_fraction,
        keep_learned=settings.hunts.smoothing.enabled,
    )


class StoredHuntSchedule:
    """Publishes ``hunt_schedule`` from the stored hunts each time the engine changes one.

    A publish that fails is logged once and retried every ``hunts.runner_poll_seconds``,
    as a runner retries on each tick, until one lands. The hunt change that triggered it
    stands either way, and with the runner scaled to zero nothing else would write the
    schedule. Publishes take turns, and each loads the hunts under the turn, so the last
    one to run reads the latest hunts and an earlier one cannot put a stale schedule back.

    Args:
        settings: The engine settings.
        client: Returns the ClickHouse client a publish runs on, called once per publish.
        rules_dir: The directory the hunts' rule files are read from.
    """

    def __init__(
        self, settings: DFESettings, *, client: Callable[[], Any], rules_dir: str | Path
    ) -> None:
        self._settings = settings
        self._client = client
        self._rules_dir = rules_dir
        self._turn = threading.Lock()
        self._timer_guard = threading.Lock()
        self._retry: threading.Timer | None = None
        self._closed = False
        self._failing = False

    def publish(self, hunt_dir: Path) -> None:
        """Bring ``hunt_schedule`` to the hunts stored in *hunt_dir*, retrying until it lands."""
        # The retry is armed or cancelled under the same turn as the publish, so an older
        # success cannot cancel the retry a newer failure armed.
        with self._turn:
            if self._try(hunt_dir):
                self._cancel_retry()
            else:
                self._retry_later(hunt_dir)

    def close(self) -> None:
        """Cancel the pending retry, and take no more."""
        self._closed = True
        self._cancel_retry()

    def _try(self, hunt_dir: Path) -> bool:
        """One publish attempt, called with the turn held. True when it landed."""
        db = self._settings.clickhouse.effective_data_database
        try:
            if not hunt_dir.is_dir():  # load_specs reads a missing directory as no hunts
                raise FileNotFoundError(f"hunt directory {hunt_dir} is missing")
            specs = load_specs(hunt_dir, **spec_sources(self._settings, db, self._rules_dir))
            publish_hunt_schedule(self._client(), db, specs, self._settings)
        except Exception as exc:  # the hunt change has already been saved, so this cannot fail it
            if not self._failing:
                logger.warning(
                    "hunt_schedule not published, retrying every runner poll",
                    error=str(exc),
                )
            self._failing = True
            return False
        self._failing = False
        return True

    def _retry_later(self, hunt_dir: Path) -> None:
        """Arm one retry. It loads the latest hunts, so it covers every change made before it runs."""
        with self._timer_guard:
            if self._retry is not None or self._closed:
                return
            self._retry = threading.Timer(
                self._settings.hunts.runner_poll_seconds, self._run_retry, args=(hunt_dir,)
            )
            self._retry.daemon = True
            self._retry.start()

    def _run_retry(self, hunt_dir: Path) -> None:
        with self._timer_guard:
            self._retry = None
        self.publish(hunt_dir)

    def _cancel_retry(self) -> None:
        with self._timer_guard:
            if self._retry is not None:
                self._retry.cancel()
                self._retry = None
