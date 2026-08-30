#  Project:      dfe-engine
#  File:         appmgmt/dryrun.py
#  Purpose:      Run an authored transform over sampled events, read-only
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Run an authored program over real events and report what it did to each one.

Syntax validation answers "does it parse". This answers "does it do what I meant
to MY data", which is the question that catches a wrong field name, a mangled
timestamp or a drop that was not intended.

Nothing here writes: no topic, no table, no commit. Events come from the sampler,
which the caller must already be entitled to use, and the program is whatever the
caller supplies. Every run is bounded on events, on output size and on time.

The backend is ``vectordotdev``, imported INSIDE the call for the reason
:mod:`dfe_engine.appmgmt.validation` gives - it links against compiled Vector
artifacts a deployment need not have, so an absent backend reports ``unavailable``
rather than raising.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum

from dfe_engine.yaml_utils import YAMLError, yaml_load_string

MAX_EVENTS = 100
"""Hard ceiling on events per run, whatever the caller asks for."""

MAX_OUTPUT_BYTES = 256 * 1024
"""Ceiling on the serialised output of one run."""

DEFAULT_TIMEOUT_SECONDS = 10.0


class DryRunStatus(StrEnum):
    """Outcome of a dry run as a whole."""

    COMPLETED = "completed"
    """The program ran; per-event results say what happened to each one."""

    UNAVAILABLE = "unavailable"
    """No backend could run it; nothing was executed."""

    DISABLED = "disabled"
    """Dry running is switched off for this deployment."""

    UNSUPPORTED = "unsupported"
    """The language, or this shape of it, cannot be run here."""

    FAILED = "failed"
    """The program could not be loaded at all - a compile error, not a data error."""


@dataclass(frozen=True, slots=True)
class EventOutcome:
    """What the program did to one event."""

    index: int
    before: str
    after: str = ""
    error: str = ""
    dropped: bool = False

    @property
    def changed(self) -> bool:
        return not self.error and not self.dropped and self.after != self.before


@dataclass(frozen=True, slots=True)
class DryRunResult:
    """The whole run: an outcome per event, plus totals."""

    status: DryRunStatus
    backend: str = ""
    message: str = ""
    events: tuple[EventOutcome, ...] = field(default_factory=tuple)
    truncated: bool = False

    @property
    def succeeded(self) -> int:
        return sum(1 for e in self.events if not e.error and not e.dropped)

    @property
    def failed(self) -> int:
        return sum(1 for e in self.events if e.error)

    @property
    def dropped(self) -> int:
        return sum(1 for e in self.events if e.dropped)


def _unavailable(message: str) -> DryRunResult:
    return DryRunResult(status=DryRunStatus.UNAVAILABLE, backend="vectordotdev", message=message)


def _run_vrl(program: str, events: list[str], timeout: float) -> DryRunResult:
    """Execute a VRL program over events through vectordotdev, if it is installed."""
    try:
        from vectordotdev.native_vector_executor import (  # ty: ignore[unresolved-import]
            quick_vrl_test,
        )
    except ImportError as exc:
        return _unavailable(f"vectordotdev is not installed: {exc}")

    try:
        raw = quick_vrl_test(program, events, max_events=len(events))
    except ImportError as exc:
        # The package importing is not proof its compiled bindings are present.
        return _unavailable(f"vectordotdev bindings are unavailable: {exc}")
    except TimeoutError:
        return DryRunResult(
            status=DryRunStatus.FAILED,
            backend="vectordotdev",
            message=f"the program did not finish within {timeout:.0f}s",
        )
    except Exception as exc:
        return _unavailable(f"vectordotdev failed to run: {exc}")

    return _interpret(raw, events)


def _interpret(raw: object, events: list[str]) -> DryRunResult:
    """Map the backend's answer onto outcomes without assuming its exact shape.

    The binding is being reworked, so accept the shapes it plausibly returns and
    report ``unavailable`` for anything unrecognised rather than inventing a result.
    """
    backend = "vectordotdev"
    if isinstance(raw, dict) and not raw.get("ok", True) and "events" not in raw:
        return DryRunResult(
            status=DryRunStatus.FAILED,
            backend=backend,
            message=str(raw.get("error") or "the program did not compile"),
        )

    rows = raw.get("events") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return _unavailable(f"unrecognised vectordotdev result: {type(raw).__name__}")

    outcomes: list[EventOutcome] = []
    for i, row in enumerate(rows[: len(events)]):
        before = events[i] if i < len(events) else ""
        outcomes.append(_outcome(i, before, row))
    return DryRunResult(status=DryRunStatus.COMPLETED, backend=backend, events=tuple(outcomes))


def _outcome(index: int, before: str, row: object) -> EventOutcome:
    """One event's result, from whichever shape the backend used for it."""
    if isinstance(row, dict) and ("error" in row or "output" in row or "dropped" in row):
        error = str(row.get("error") or "")
        if error:
            return EventOutcome(index=index, before=before, error=error)
        if row.get("dropped"):
            return EventOutcome(index=index, before=before, dropped=True)
        return EventOutcome(index=index, before=before, after=_as_text(row.get("output")))
    if row is None:
        return EventOutcome(index=index, before=before, dropped=True)
    return EventOutcome(index=index, before=before, after=_as_text(row))


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _vector_remap_source(content: str) -> tuple[str, str]:
    """The VRL inside a Vector transform file, or why there is none to run.

    A file is Vector's own shape - a ``transforms`` map of named components, which
    is what dfe-transform-vector loads and passes through to Vector verbatim. Only
    ``remap`` carries a VRL program; every other type is a component whose
    behaviour lives in Vector itself, so running it needs Vector rather than a VRL
    interpreter.

    Several remaps in one file are concatenated in declaration order, which is the
    order Vector would run them in when they are chained.
    """
    try:
        parsed = yaml_load_string(content)
    except YAMLError as exc:
        return "", f"not valid YAML: {exc}"
    if not isinstance(parsed, dict):
        return "", "a Vector transform file must be a mapping"
    components = parsed.get("transforms")
    if not isinstance(components, dict) or not components:
        return "", "the file declares no 'transforms' components"

    programs: list[str] = []
    skipped: list[str] = []
    for name, component in components.items():
        if not isinstance(component, dict):
            return "", f"transform {name!r} is not a mapping"
        kind = str(component.get("type", ""))
        if kind != "remap":
            skipped.append(f"{name} ({kind or 'typeless'})")
            continue
        source = component.get("source")
        if not isinstance(source, str) or not source.strip():
            return "", f"remap {name!r} declares no inline 'source' program"
        programs.append(source)

    if not programs:
        return "", f"no remap to run; the file declares {', '.join(skipped)}"
    return "\n".join(programs), ""


def run_language(
    language: str,
    program: str,
    events: list[str],
    *,
    enabled: bool,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> DryRunResult:
    """Run a program written in ``language`` over ``events``.

    ``enabled`` is the deployment's switch; off reports ``disabled`` rather than
    silently returning nothing, so a caller can tell "ran and did nothing" from
    "never ran". Events are capped at :data:`MAX_EVENTS` and the result is marked
    truncated when the cap bites.
    """
    if not enabled:
        return DryRunResult(status=DryRunStatus.DISABLED)
    if not events:
        return DryRunResult(status=DryRunStatus.COMPLETED, message="no events to run over")

    capped = events[:MAX_EVENTS]
    truncated = len(events) > len(capped)

    if language == "vrl":
        source = program
    elif language == "yaml":
        source, why = _vector_remap_source(program)
        if why:
            return DryRunResult(status=DryRunStatus.UNSUPPORTED, message=why)
    else:
        return DryRunResult(
            status=DryRunStatus.UNSUPPORTED, message=f"no dry-run backend for {language!r}"
        )

    result = _run_vrl(source, capped, timeout)
    return _bounded(result, truncated)


def _bounded(result: DryRunResult, truncated: bool) -> DryRunResult:
    """Drop trailing events once the serialised output passes the size ceiling."""
    if result.status is not DryRunStatus.COMPLETED:
        return result
    kept: list[EventOutcome] = []
    size = 0
    for outcome in result.events:
        size += len(outcome.before) + len(outcome.after) + len(outcome.error)
        if size > MAX_OUTPUT_BYTES:
            truncated = True
            break
        kept.append(outcome)
    return DryRunResult(
        status=result.status,
        backend=result.backend,
        message=result.message,
        events=tuple(kept),
        truncated=truncated,
    )
