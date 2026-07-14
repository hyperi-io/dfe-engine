#  Project:      dfe-engine
#  File:         hunt_runner/spec_loader.py
#  Purpose:      Load gitops hunt YAML defs -> HuntSpec (rate-only in v1)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Load hunt definitions from the gitops config dir into HuntSpec objects.

The pull-based hunt runner ticks over HuntSpec objects; this is the file->model
bridge. Each *.yaml in the hunt dir becomes ONE HuntSpec keyed by its filename stem
(the hunt_id) - filenames are the identity, never an in-file id field (the YAML-1.1
gotcha: 'off'/'yes'/'no' inside YAML coerce to booleans, so identity must not live
there). v1 handles the "rate" schedule mode only; "anchored" hunts are recognised
and skipped (a later phase adds them). One bad file never sinks the whole load.

NOTE: building `query` from a hunt's `rules` (the rule->SQL compiler) is OUT OF
SCOPE here. v1 reads a direct `query` field, which suits synthetic/test hunts and
pre-compiled hunts. That is a deliberate v1 boundary, not an oversight.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.yaml_utils import yaml_load

from .interval import parse_interval
from .models import HuntSpec


def _resolve_schedule_value(definition: dict[str, Any], stem: str) -> str | int | None:
    """Reduce a hunt def's schedule to a single parse_interval() input value.

    Returns the value to feed parse_interval (a duration/cron string, an int, or a
    list-min already reduced to seconds), or None if the hunt must be skipped (an
    anchored schedule - logged here). Prefers the new `schedule` block; falls back to
    the legacy flat `cron` field (string or list of cron strings -> take the MIN gap
    so we fire at least as often as the tightest schedule).
    """
    schedule = definition.get("schedule")
    if isinstance(schedule, dict):
        mode = schedule.get("mode", "rate")
        if mode != "rate":
            logger.info(f"anchored hunt {stem} skipped (rate-only in v1)")
            return None
        # rate: an explicit interval wins; else the cron alias.
        if schedule.get("interval") is not None:
            return schedule["interval"]
        return schedule.get("cron")

    # Legacy flat cron: a single string, or a list of cron strings.
    cron = definition.get("cron")
    if isinstance(cron, list):
        # Fire at least as often as the tightest member -> the MIN interval.
        return min(parse_interval(entry) for entry in cron)
    return cron


def _build_spec(definition: dict[str, Any], stem: str) -> HuntSpec | None:
    """Build one HuntSpec from a parsed hunt def, or None if it must be skipped.

    Skips (returns None) for anchored schedules and non-positive intervals; a
    non-positive interval would divide-by-zero in the phase-offset spread maths.
    """
    value = _resolve_schedule_value(definition, stem)
    if value is None:
        return None  # anchored (already logged) or no schedule at all

    interval_seconds = parse_interval(value)
    if interval_seconds <= 0:
        logger.warning(f"skipping hunt {stem}: non-positive interval {interval_seconds}")
        return None

    return HuntSpec(
        hunt_id=stem,
        interval_seconds=interval_seconds,
        query=str(definition.get("query", "")),
        target_table=str(definition.get("global_target_table_name", definition.get("target", ""))),
        timestamp_field=str(definition.get("timestamp_field", "timestamp_load")),
    )


def load_specs(hunt_dir: str | Path) -> dict[str, HuntSpec]:
    """Load every *.yaml hunt def in hunt_dir into HuntSpec objects keyed by hunt_id.

    The hunt_id is the filename STEM (identity is the filename, never an in-file id).
    A missing directory returns an empty dict (logged at debug). Each file is wrapped
    in try/except so one malformed hunt is skipped (logged) without breaking the load
    of the rest. Anchored and non-positive-interval hunts are skipped too.
    """
    directory = Path(hunt_dir)
    if not directory.is_dir():
        logger.debug(f"hunt dir {directory} does not exist; no specs loaded")
        return {}

    specs: dict[str, HuntSpec] = {}
    for path in sorted(directory.glob("*.yaml")):
        stem = path.stem
        try:
            definition = yaml_load(path)
            if not isinstance(definition, dict):
                logger.warning(f"skipping hunt {stem}: not a YAML mapping")
                continue
            spec = _build_spec(definition, stem)
            if spec is not None:
                specs[stem] = spec
        except Exception as exc:  # one bad file must never break the whole load
            logger.warning(f"skipping hunt {stem}: {exc}")
            continue

    return specs
