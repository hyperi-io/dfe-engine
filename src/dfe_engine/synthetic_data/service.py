#  Project:      dfe-engine
#  File:         synthetic_data/service.py
#  Purpose:      Synthetic data orchestration - pack listing, bounded generate, stream tasks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Synthetic data service - the API-facing orchestration layer.

Resolves schema refs against the schemas root (the dfe-schemas package or
``DFE_SCHEMAS_DIR``), enforces the settings ceilings (count, rate, stream
duration), and runs stream deliveries as TaskManager coroutines with
progress reporting. Refs are containment-checked against the schemas root,
so a traversal ref can never read outside it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from scalo.logger import logger

from dfe_engine.schema.schema_loader import (
    SchemaLoader,
    SchemaLoadError,
    _resolve_schemas_root,
    resolve_schema_yaml_path,
)
from dfe_engine.synthetic_data.models import (
    GenerateRequest,
    GenerateResult,
    LookalikeRequest,
    PackInfo,
    StreamRequest,
    StreamSummary,
    SyntheticDataError,
)
from dfe_engine.synthetic_data.sample_source import SampleEventFactory
from dfe_engine.synthetic_data.schema_source import SchemaEventFactory, _provider_from_path
from dfe_engine.synthetic_data.stream import HttpPostSink, stream_events

if TYPE_CHECKING:
    from dfe_engine.settings import SyntheticDataSettings


class SyntheticDataService:
    """Pack discovery + bounded generation against the configured ceilings."""

    def __init__(self, settings: SyntheticDataSettings) -> None:
        self.settings = settings

    # ── schema resolution ─────────────────────────────────────────

    def _schemas_root(self) -> Path:
        root = _resolve_schemas_root()
        if root is None:
            raise SyntheticDataError(
                "No schemas root available (dfe-schemas package or DFE_SCHEMAS_DIR)"
            )
        return root

    def resolve_ref(self, ref: str) -> Path:
        """Resolve a schema ref to a YAML path inside the schemas root."""
        root = self._schemas_root()
        path = resolve_schema_yaml_path(root, ref)
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise SyntheticDataError(f"Schema ref {ref!r} escapes the schemas root")
        if not resolved.exists():
            raise SyntheticDataError(f"Schema ref {ref!r} not found under {root}")
        return resolved

    def list_packs(self) -> list[PackInfo]:
        """List meta schemas the generator can drive (scenario/hint counts included)."""
        root = self._schemas_root()
        packs: list[PackInfo] = []
        for path in sorted((root / "meta").rglob("*.yaml")):
            ref = path.relative_to(root).with_suffix("").as_posix()
            try:
                entry = SchemaLoader.load_version_entry(path)
            except SchemaLoadError as exc:
                logger.warning(f"synthetic-data: skipping unreadable schema {ref}: {exc}")
                continue
            columns = [c for c in entry.get("columns", []) if isinstance(c, dict)]
            source_cols = [c for c in columns if str(c.get("expr", "")).startswith("@source")]
            scenarios = (entry.get("synthetic") or {}).get("scenarios") or []
            packs.append(
                PackInfo(
                    ref=ref,
                    provider=_provider_from_path(path),
                    columns=len(source_cols),
                    scenarios=len(scenarios),
                    hinted_columns=sum(1 for c in columns if c.get("synthetic")),
                )
            )
        return packs

    def build_factory(
        self,
        ref: str,
        *,
        version: str | None = None,
        seed: int | None = None,
        tags: dict[str, Any] | None = None,
        mark_synthetic: bool = True,
    ) -> SchemaEventFactory:
        """Build an event factory for a resolved schema ref."""
        path = self.resolve_ref(ref)
        return SchemaEventFactory.from_schema(
            path, version=version, seed=seed, tags=tags, mark_synthetic=mark_synthetic
        )

    # ── operations ────────────────────────────────────────────────

    def generate(self, req: GenerateRequest) -> GenerateResult:
        """Generate a bounded batch inline."""
        if req.count > self.settings.max_count:
            raise SyntheticDataError(
                f"count {req.count} exceeds the configured ceiling {self.settings.max_count}"
            )
        factory = self.build_factory(
            req.schema_ref,
            version=req.version,
            seed=req.seed,
            tags=req.tags,
            mark_synthetic=req.mark_synthetic,
        )
        rate = min(req.rate_eps or self.settings.default_rate_eps, self.settings.max_rate_eps)
        events = factory.events(req.count, end=datetime.now(UTC), rate_eps=rate)
        return GenerateResult(
            schema_ref=req.schema_ref,
            version=req.version,
            count=len(events),
            seed=req.seed,
            events=events,
        )

    def generate_lookalike(self, req: LookalikeRequest) -> GenerateResult:
        """Generate a bounded lookalike batch from a supplied sample."""
        if req.count > self.settings.max_count:
            raise SyntheticDataError(
                f"count {req.count} exceeds the configured ceiling {self.settings.max_count}"
            )
        if not (req.rows or req.lines):
            raise SyntheticDataError("lookalike needs sample input: rows and/or lines")
        factory = SampleEventFactory(
            rows=req.rows,
            lines=req.lines,
            seed=req.seed,
            tags=req.tags,
            mark_synthetic=req.mark_synthetic,
        )
        rate = min(req.rate_eps or self.settings.default_rate_eps, self.settings.max_rate_eps)
        events = factory.events(req.count, end=datetime.now(UTC), rate_eps=rate)
        return GenerateResult(schema_ref="sample", count=len(events), seed=req.seed, events=events)

    def validate_stream(self, req: StreamRequest) -> None:
        """Fail fast on a bad stream request (before it becomes a task)."""
        if req.count is None and req.duration_s is None:
            raise SyntheticDataError("stream needs a bound: count and/or duration_s")
        if req.count is not None and req.count > self.settings.max_count:
            raise SyntheticDataError(
                f"count {req.count} exceeds the configured ceiling {self.settings.max_count}"
            )
        if req.duration_s is not None and req.duration_s > self.settings.max_stream_seconds:
            raise SyntheticDataError(
                f"duration_s {req.duration_s} exceeds the configured ceiling "
                f"{self.settings.max_stream_seconds}"
            )
        if not req.receiver_url.startswith(("http://", "https://")):
            raise SyntheticDataError("receiver_url must be http(s)")
        allowed = [h.strip() for h in self.settings.allowed_receiver_hosts.split(",") if h.strip()]
        if allowed:
            host = urlsplit(req.receiver_url).hostname or ""
            if host not in allowed:
                raise SyntheticDataError(
                    f"receiver host {host!r} is not in synthetic_data.allowed_receiver_hosts"
                )
        self.resolve_ref(req.schema_ref)

    async def run_stream(self, req: StreamRequest, *, task: Any = None) -> dict[str, Any]:
        """Stream to the receiver as a TaskManager coroutine; returns the summary."""
        factory = self.build_factory(
            req.schema_ref,
            version=req.version,
            seed=req.seed,
            tags=req.tags,
            mark_synthetic=req.mark_synthetic,
        )
        sink = HttpPostSink(
            req.receiver_url,
            headers=req.headers,
            batch_max=req.batch_max,
            ndjson=req.ndjson,
        )
        rate = min(req.rate_eps or self.settings.default_rate_eps, self.settings.max_rate_eps)
        started = time.monotonic()

        emitted = await stream_events(
            factory,
            _ProgressSink(sink, task, started, req),
            rate_eps=rate,
            count=req.count,
            duration_s=req.duration_s,
        )
        return StreamSummary(
            schema_ref=req.schema_ref,
            receiver_url=req.receiver_url,
            emitted=emitted,
            sent=sink.sent,
            failed=sink.failed,
            seconds=round(time.monotonic() - started, 1),
        ).model_dump(by_alias=True)


class _ProgressSink:
    """Wraps the delivery sink to report task progress every 25 events."""

    def __init__(self, base: HttpPostSink, task: Any, started: float, req: StreamRequest) -> None:
        self._base = base
        self._task = task
        self._started = started
        self._req = req
        self._emitted = 0

    async def __call__(self, event: dict[str, Any]) -> None:
        await self._base(event)
        self._emitted += 1
        if self._task is not None and self._emitted % 25 == 0:
            self._task.set_progress(
                _progress(self._emitted, self._started, self._req),
                f"{self._emitted} events ({self._base.failed} failed)",
            )

    async def flush(self) -> None:
        await self._base.flush()

    async def aclose(self) -> None:
        await self._base.aclose()


def _progress(emitted: int, started: float, req: StreamRequest) -> int:
    if req.count:
        return min(99, int(emitted * 100 / req.count))
    if req.duration_s:
        return min(99, int((time.monotonic() - started) * 100 / req.duration_s))
    return 0
