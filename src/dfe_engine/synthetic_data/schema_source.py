#  Project:      dfe-engine
#  File:         synthetic_data/schema_source.py
#  Purpose:      Meta-schema-driven event factory (reference packs)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Meta-schema-driven event factory.

Inverts a dfe-schemas meta schema: each column's ``@source:`` expr names where
the value lives in the SOURCE JSON (the shape dfe-receiver ingests), so the
factory builds events by writing a generated value at every ``@source`` path.
Column semantics come from the heuristic classifier, overridden per column by
optional ``synthetic:`` hints authored in the schema YAML.

Every event is marked ``tags.synthetic: true`` by default (the common header
maps ``first(tags/_tags/...)`` into ``_tags``), so synthetic data is always
distinguishable downstream. Overridable for the rare demo that must not show
the marker.

Path grammar accepted (the closed set observed across dfe-schemas):
``Key``, ``dotted.path``, ``Resources[0].Type``, ``first(a/b)`` (first
alternative wins), and keys containing spaces (``Report Refresh Date``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.source.models import SchemaColumn
from dfe_engine.synthetic_data.entities import EntityPool
from dfe_engine.synthetic_data.models import ColumnHints, Scenario, SyntheticDataError
from dfe_engine.synthetic_data.values import (
    EventContext,
    Inference,
    classify,
    generate,
    paced_timestamps,
    render_timestamp,
)

_SOURCE_PREFIX = "@source:"
_SEGMENT_RE = re.compile(r"^(?P<key>[^\[\]]+)(?:\[(?P<idx>\d+)\])?$")


@dataclass(frozen=True, slots=True)
class _PathSegment:
    key: str
    index: int | None = None


def parse_source_path(expr: str | None) -> list[_PathSegment] | None:
    """Parse an ``@source:`` expr into path segments.

    Returns None for non-source exprs (``@generated``, ``@computed``, ...).
    ``first(a/b)`` collapses to its first alternative - the preferred source
    spelling is listed first in the schemas.

    Raises:
        SyntheticDataError: If the expr is ``@source`` but the path is malformed.
    """
    if not expr or not expr.strip().startswith(_SOURCE_PREFIX):
        return None
    path = expr.strip()[len(_SOURCE_PREFIX) :].strip()
    if path.startswith("first(") and path.endswith(")"):
        path = path[len("first(") : -1].split("/")[0].strip()
    segments: list[_PathSegment] = []
    for raw in path.split("."):
        m = _SEGMENT_RE.match(raw.strip())
        if not m or not m.group("key").strip():
            raise SyntheticDataError(f"Unparseable @source path segment {raw!r} in {expr!r}")
        idx = m.group("idx")
        segments.append(_PathSegment(m.group("key").strip(), int(idx) if idx else None))
    if not segments:
        raise SyntheticDataError(f"Empty @source path in {expr!r}")
    return segments


def set_path(event: dict[str, Any], segments: list[_PathSegment], value: Any) -> None:
    """Write ``value`` into ``event`` at the parsed path, creating containers."""
    node: Any = event
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if seg.index is None:
            if last:
                node[seg.key] = value
            else:
                node = node.setdefault(seg.key, {})
        else:
            arr = node.setdefault(seg.key, [])
            while len(arr) <= seg.index:
                arr.append({})
            if last:
                arr[seg.index] = value
            else:
                node = arr[seg.index]


def load_synthetic_hints(
    schema_path: str | Path, *, version: str | None = None
) -> dict[str, ColumnHints]:
    """Read per-column ``synthetic:`` hints from a schema YAML.

    The schema loader's ``SchemaColumn`` model ignores unknown keys, so hints
    ride in the same file without affecting DDL or validation. Returns a map
    of column name -> hints for columns that declare any.
    """
    hints: dict[str, ColumnHints] = {}
    for raw in SchemaLoader.load_raw_columns(schema_path, version=version):
        block = raw.get("synthetic")
        if block is None:
            continue
        name = raw.get("name", "?")
        try:
            hints[name] = ColumnHints.model_validate(block)
        except Exception as e:
            raise SyntheticDataError(
                f"Invalid synthetic data hints on column {name!r} in {schema_path}: {e}"
            ) from e
    return hints


def load_synthetic_scenarios(
    schema_path: str | Path, *, version: str | None = None
) -> list[Scenario]:
    """Read schema-level ``synthetic.scenarios`` from a schema YAML version entry.

    Scenarios live beside ``columns`` in the version entry and keep
    correlated columns coherent (one weighted draw per event). Returns an
    empty list when the schema declares none.
    """
    entry = SchemaLoader.load_version_entry(schema_path, version=version)
    block = entry.get("synthetic") or {}
    scenarios = block.get("scenarios") or []
    result: list[Scenario] = []
    for i, raw in enumerate(scenarios):
        try:
            result.append(Scenario.model_validate(raw))
        except Exception as e:
            raise SyntheticDataError(
                f"Invalid synthetic data scenario {i} in {schema_path}: {e}"
            ) from e
    return result


def _provider_from_path(schema_path: str | Path) -> str:
    parts = {p.lower() for p in Path(schema_path).parts}
    for provider in ("aws", "azure", "gcp", "m365"):
        if provider in parts:
            return "azure" if provider == "m365" else provider
    return "aws"


class SchemaEventFactory:
    """Generates source-shaped events from a dfe-schemas meta schema.

    Args:
        columns: Schema columns (``SchemaLoader.load_columns`` output).
        seed: Determinism seed; identical seed => identical events.
        pool: Entity pool to draw from (built from ``seed`` when omitted).
        provider: Cloud provider vocabulary hint (``aws``/``azure``/``gcp``).
        hints: Per-column ``synthetic:`` hints (``load_synthetic_hints`` output).
        scenarios: Schema-level coherent shapes (``load_synthetic_scenarios``
            output); one is drawn per event and overrides hints for the
            columns it names.
        tags: Extra tags merged into the event's ``tags`` object.
        mark_synthetic: Emit ``tags.synthetic: true`` (default on).
    """

    def __init__(
        self,
        columns: list[SchemaColumn],
        *,
        seed: int | None = None,
        pool: EntityPool | None = None,
        provider: str = "aws",
        hints: dict[str, ColumnHints] | None = None,
        scenarios: list[Scenario] | None = None,
        tags: dict[str, Any] | None = None,
        mark_synthetic: bool = True,
    ) -> None:
        self.pool = pool or EntityPool(seed)
        self.hints = hints or {}
        self.scenarios = scenarios or []
        self._scenario_weights = [s.weight for s in self.scenarios]
        self.tags = dict(tags or {})
        if mark_synthetic:
            self.tags.setdefault("synthetic", True)
        self._plan: list[tuple[SchemaColumn, list[_PathSegment], Inference]] = []
        for col in columns:
            segments = parse_source_path(col.expr or "")
            if segments is None:
                continue
            self._plan.append((col, segments, classify(col, provider=provider)))
        if not self._plan:
            raise SyntheticDataError("Schema has no @source columns - nothing to generate")

    @classmethod
    def from_schema(
        cls,
        schema_path: str | Path,
        *,
        version: str | None = None,
        seed: int | None = None,
        pool: EntityPool | None = None,
        tags: dict[str, Any] | None = None,
        mark_synthetic: bool = True,
    ) -> SchemaEventFactory:
        """Build a factory straight from a schema YAML path."""
        columns = SchemaLoader.load_columns(schema_path, version=version)
        return cls(
            columns,
            seed=seed,
            pool=pool,
            provider=_provider_from_path(schema_path),
            hints=load_synthetic_hints(schema_path, version=version),
            scenarios=load_synthetic_scenarios(schema_path, version=version),
            tags=tags,
            mark_synthetic=mark_synthetic,
        )

    # ── event construction ────────────────────────────────────────

    def event(self, when: datetime | None = None) -> dict[str, Any]:
        """Generate one source-shaped event.

        Args:
            when: Event timestamp (defaults to now UTC) - streams pass their
                pacing clock so timestamps match the emission cadence.
        """
        pool = self.pool
        account = pool.account()
        ctx = EventContext(
            pool=pool,
            host=pool.host(),
            user=pool.user(),
            account=account,
            region=pool.rng.choice(account.regions),
            when=when or datetime.now(UTC),
        )
        scenario: Scenario | None = None
        if self.scenarios:
            scenario = pool.rng.choices(self.scenarios, weights=self._scenario_weights, k=1)[0]
        event: dict[str, Any] = {}
        for col, segments, inference in self._plan:
            set_path(event, segments, self._value(col, inference, ctx, scenario))
        if self.tags:
            tags_node = event.setdefault("tags", {})
            for key, value in self.tags.items():
                tags_node.setdefault(key, value)
        return event

    def events(
        self, count: int, *, end: datetime | None = None, rate_eps: float = 5.0
    ) -> list[dict[str, Any]]:
        """Generate a batch whose timestamps read as a live tail.

        Timestamps are spaced by a Poisson process at ``rate_eps`` ending at
        ``end`` (default now), oldest first - so a batch insert still looks
        like a stream that has been running.
        """
        if count < 1:
            raise SyntheticDataError("count must be >= 1")
        stamps = paced_timestamps(self.pool.rng, count, end or datetime.now(UTC), rate_eps)
        return [self.event(when=stamp) for stamp in stamps]

    def _value(
        self,
        col: SchemaColumn,
        inference: Inference,
        ctx: EventContext,
        scenario: Scenario | None = None,
    ) -> Any:
        if scenario is not None:
            if col.name in scenario.templates:
                return self._fill(ctx.pool.rng.choice(scenario.templates[col.name]), ctx, col.name)
            if col.name in scenario.values:
                value = scenario.values[col.name]
                return ctx.pool.rng.choice(value) if isinstance(value, list) else value
        hint = self.hints.get(col.name)
        if hint is None:
            return generate(inference, ctx)
        if hint.static is not None:
            return hint.static
        if hint.values is not None:
            return ctx.pool.rng.choices(hint.values, weights=hint.weights, k=1)[0]
        if hint.templates is not None:
            return self._fill(ctx.pool.rng.choice(hint.templates), ctx, col.name)
        if hint.provider is not None:
            method = getattr(ctx.pool.fake, hint.provider, None)
            if method is None:
                raise SyntheticDataError(
                    f"Unknown faker provider {hint.provider!r} on column {col.name!r}"
                )
            return method()
        if hint.minimum is not None or hint.maximum is not None:
            low = hint.minimum or 0.0
            high = hint.maximum if hint.maximum is not None else low + 100.0
            if (col.type or "").lower() in {"integer", "int", "long"}:
                return ctx.pool.rng.randint(int(low), int(high))
            return round(ctx.pool.rng.uniform(low, high), 2)
        if hint.format is not None:
            return render_timestamp(ctx.when, hint.format)
        logger.warning(
            f"synthetic data hints on column {col.name!r} set nothing usable - falling back"
        )
        return generate(inference, ctx)

    @staticmethod
    def _fill(template: str, ctx: EventContext, column_name: str) -> str:
        try:
            return template.format_map(ctx.template_map())
        except KeyError as e:
            raise SyntheticDataError(
                f"Unknown template placeholder {e} on column {column_name!r}"
            ) from e
