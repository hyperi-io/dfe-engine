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

A hunt states what it runs one of two ways: a direct `query` (pre-compiled, one
statement) or `rules` (rule YAML names), which is what the API writes. Rules are
compiled here, through rule_compiler, so a hunt made in the UI runs without anyone
hand-editing its YAML.

Only a compiled rule carries the per-run detection cap. A direct `query` is SQL the
author wrote whole, so no LIMIT can be put into it safely; it runs uncapped, and
the load says so.

A direct `query` is put through the same refusal as a rule's condition: one that
calls a ClickHouse function reading outside the row or sending it to another
service drops its hunt, logged. It runs as a user that may write, so ClickHouse
cannot be asked to refuse the call.
"""

from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.hunts.rule_guard import refuse_offbox_calls
from dfe_engine.settings import MAX_DETECTIONS_PER_RUN
from dfe_engine.yaml_utils import yaml_load

from .checkpoint import TIMESTAMP_FIELD
from .interval import parse_interval
from .models import HuntSpec, HuntStatement
from .rule_compiler import compile_hunt_queries


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


def interval_seconds_for(definition: dict[str, Any], stem: str = "") -> int | None:
    """The hunt's firing interval in seconds, or None when it has no rate schedule.

    Public so the API can answer "next due" from the SAME cron reading the runner
    schedules by. A second reading of cron would drift from the one that fires.
    """
    value = _resolve_schedule_value(definition, stem)
    if value is None:
        return None
    try:
        interval = parse_interval(value)
    except Exception:
        return None
    return interval if interval > 0 else None


def _timestamp_field(definition: dict[str, Any]) -> str:
    """The hunt's watermark column. `checkpoint_timestamp_field` is the API's name for it."""
    for key in ("timestamp_field", "checkpoint_timestamp_field"):
        value = definition.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return TIMESTAMP_FIELD


def _build_spec(
    definition: dict[str, Any],
    stem: str,
    rules_dir: str | Path,
    default_target: str,
    max_detections: int,
) -> HuntSpec | None:
    """Build one HuntSpec from a parsed hunt def, or None if it must be skipped.

    Skips (returns None) for anchored schedules and non-positive intervals; a
    non-positive interval would divide-by-zero in the phase-offset spread maths.
    Skips a direct ``query`` that calls a function reading outside the row.
    """
    value = _resolve_schedule_value(definition, stem)
    if value is None:
        return None  # anchored (already logged) or no schedule at all

    interval_seconds = parse_interval(value)
    if interval_seconds <= 0:
        logger.warning(f"skipping hunt {stem}: non-positive interval {interval_seconds}")
        return None

    query = str(definition.get("query", "")).strip()
    if query:
        # A hunt YAML committed straight into the deploy repo never passes the API.
        refusal = refuse_offbox_calls(query, subject="A hunt's query")
        if refusal is not None:
            logger.error(f"skipping hunt {stem}: query refused: {refusal}")
            return None
        logger.info(
            "hunt runs a direct query, so the per-run detection cap does not apply",
            hunt_id=stem,
        )
        queries = [HuntStatement(sql=query)]
    else:
        queries = compile_hunt_queries(
            definition,
            stem,
            rules_dir=rules_dir,
            default_target=default_target,
            max_detections=max_detections,
        )

    return HuntSpec(
        hunt_id=stem,
        interval_seconds=interval_seconds,
        queries=queries,
        target_table=str(definition.get("global_target_table_name", definition.get("target", ""))),
        timestamp_field=_timestamp_field(definition),
    )


def load_specs(
    hunt_dir: str | Path,
    *,
    rules_dir: str | Path = "",
    default_target: str = "",
    max_detections: int = MAX_DETECTIONS_PER_RUN,
) -> dict[str, HuntSpec]:
    """Load every *.yaml hunt def in hunt_dir into HuntSpec objects keyed by hunt_id.

    The hunt_id is the filename STEM (identity is the filename, never an in-file id).
    A missing directory returns an empty dict (logged at debug). Each file is wrapped
    in try/except so one malformed hunt is skipped (logged) without breaking the load
    of the rest. Anchored and non-positive-interval hunts are skipped too.

    Args:
        hunt_dir: Directory of hunt YAML files (``hunts.hunt_dir``).
        rules_dir: Directory of rule YAML files (``hunts.rules_dir``), read when a
            hunt names `rules` instead of carrying a `query`.
        default_target: ``db.table`` a compiled rule writes to when neither the rule
            entry nor the hunt names one.
        max_detections: Detection rows each compiled rule may write in one run.
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
            spec = _build_spec(definition, stem, rules_dir, default_target, max_detections)
            if spec is not None:
                specs[stem] = spec
        except Exception as exc:  # one bad file must never break the whole load
            logger.warning(f"skipping hunt {stem}: {exc}")
            continue

    return specs
