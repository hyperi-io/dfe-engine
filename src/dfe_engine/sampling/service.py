#  Project:      dfe-engine
#  File:         sampling/service.py
#  Purpose:      Sampler orchestration - dispatch modes, gate logreducer, format
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The Sampler service.

One entry point - ``Sampler.run`` - shared by the API router and the CLI. It
resolves a request's target (a registered source's CH table / land topic, or an
explicit override), dispatches to the mode's reader, and formats the result with
both raw ``lines`` and parsed ``rows`` + discovered ``keys``.

logreducer ("smart"/"anomaly") is memory-hungry, so it runs behind an
asyncio.Semaphore capped at ``sampler.max_concurrent`` and each run is bounded
to ``sampler.max_memory_gb``. logreducer is imported lazily inside those paths
so the engine runs (and the fast modes work) even before it is installed.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from scalo.logger import logger

from dfe_engine.ai.sampling import discover_json_keys
from dfe_engine.source.registry import SourceNotFoundError

from . import clickhouse_reader as ch_reader
from . import kafka_reader as kr
from .models import (
    GATED_MODES,
    SampleBackend,
    SampleMode,
    SampleRequest,
    SamplerError,
    SampleResult,
)

# An explicit ``table`` override is interpolated into ``FROM {target}`` on the
# shared ADMIN client (ClickHouse cannot bind an identifier), so it MUST be a
# bare - optionally backtick-quoted - ``db.table``, never free SQL. Without this a
# caller could pass ``(SELECT ... FROM dfe_internal.repository) AS t`` and read
# every org past any row policy (F-SAMPLER-SQLI). The router additionally gates
# any table/filter behind ``query:raw``; this is the identifier-shape check that
# also protects the CLI path. Accept ``db.table``/``table`` with each part a bare
# identifier or a backtick-quoted one; reject whitespace, quotes-other-than-bare
# backticks, parens, commas, semicolons and operators.
_IDENT = r"(?:`[A-Za-z_][A-Za-z0-9_]*`|[A-Za-z_][A-Za-z0-9_]*)"
_SAFE_TABLE = re.compile(rf"^{_IDENT}(?:\.{_IDENT})?$")


def _validate_table_override(table: str) -> str:
    """Return ``table`` if it is a bare db.table identifier, else raise.

    See ``_SAFE_TABLE`` - this is the sole gate turning caller-supplied
    ``req.table`` into an interpolated FROM target (F-SAMPLER-SQLI).
    """
    if not _SAFE_TABLE.match(table):
        raise SamplerError(
            f"Invalid table {table!r}: must be a bare db.table identifier "
            "(optionally backtick-quoted), not free SQL"
        )
    return table


class Sampler:
    """Stateless sampler holding only the config + the concurrency gate.

    Instantiate once (app-level) so the semaphore is shared across requests.
    """

    def __init__(
        self, sampler_settings: Any, kafka_settings: Any, clickhouse_settings: Any
    ) -> None:
        self._cfg = sampler_settings
        self._kafka = kafka_settings
        self._ch = clickhouse_settings
        # Created lazily on first gated run so it binds to the running loop.
        self._gate: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._gate is None:
            self._gate = asyncio.Semaphore(self._cfg.max_concurrent)
        return self._gate

    # ── public entry point ─────────────────────────────────────

    async def run(
        self, req: SampleRequest, ch: Any, source_registry: Any, *, task: Any = None
    ) -> dict:
        """Execute a sample request. Returns ``SampleResult`` as a dict.

        ``task`` is the TaskManager handle (for progress); optional so the CLI
        can call this directly without a task.
        """
        limit = min(req.limit or self._cfg.default_limit, self._cfg.max_limit)
        _progress(task, 10, "Resolving target")
        target, source_label = self._resolve_target(req, source_registry)

        gated = req.mode in GATED_MODES
        _progress(task, 30, f"Sampling ({req.mode.value}, {req.backend.value})")
        if gated:
            lines, stats, note = await self._run_gated(req, ch, target, source_label, limit, task)
        else:
            lines, stats, note = await asyncio.to_thread(
                self._fast_sample, req, ch, target, source_label, limit
            )

        _progress(task, 85, "Formatting result")
        result = self._format(req, target, limit, lines, stats, note)
        _progress(task, 100, "Done")
        return result.model_dump()

    async def _run_gated(
        self,
        req: SampleRequest,
        ch: Any,
        target: str,
        source_label: str | None,
        limit: int,
        task: Any,
    ) -> tuple[list[str], dict[str, Any], str | None]:
        """Run a gated (logreducer) sample under the memory gate.

        WHY the manual acquire + shield + done-callback release: asyncio.to_thread
        is NOT cancellable. If this coroutine is cancelled (client disconnect /
        task cancel) the logreducer worker thread keeps running and holding its
        max_memory_gb budget. Releasing the gate on cancel (as ``async with
        semaphore`` would) lets another gated run start while the old thread is
        still resident, blowing past the max_concurrent x max_memory_gb ceiling.
        So: acquire the gate, shield the worker so cancelling us never marks it
        done early, and release ONLY from a done-callback that fires when the
        thread genuinely finishes (success or error).
        """
        sem = self._semaphore()
        await sem.acquire()
        _progress(task, 40, "Running logreducer")
        worker = asyncio.ensure_future(
            asyncio.to_thread(self._reduce_sample, req, ch, target, source_label, limit)
        )
        worker.add_done_callback(lambda _f: sem.release())
        return await asyncio.shield(worker)

    def resolve_or_raise(self, req: SampleRequest, source_registry: Any) -> str:
        """Validate the request's target up front, returning the resolved target.

        Lets the caller reject a bad request (missing source, no table/topic)
        synchronously instead of via a failed background task. Raises
        ``SamplerError``.
        """
        target, _ = self._resolve_target(req, source_registry)
        return target

    # ── target resolution ──────────────────────────────────────

    def _resolve_target(self, req: SampleRequest, source_registry: Any) -> tuple[str, str | None]:
        """Return (target, source_label) for the request.

        ``source_label`` is a ``_source`` filter, set only when sampling the
        shared landing table via an explicit table override + source name.
        """
        if req.backend == SampleBackend.CLICKHOUSE:
            if req.table:
                return _validate_table_override(req.table), req.source
            if req.source:
                src = self._get_source(source_registry, req.source)
                db = self._ch.effective_data_database
                return f"`{db}`.`{src.table_name}`", None
            raise SamplerError("ClickHouse sampling needs a 'source' or an explicit 'table'")
        # Kafka
        if req.topic:
            return req.topic, None
        if req.source:
            src = self._get_source(source_registry, req.source)
            return src.topic_land, None
        raise SamplerError("Kafka sampling needs a 'source' or an explicit 'topic'")

    @staticmethod
    def _get_source(source_registry: Any, name: str) -> Any:
        if source_registry is None:
            raise SamplerError("Source registry unavailable - pass an explicit table/topic")
        try:
            return source_registry.get_source(name)
        except SourceNotFoundError as exc:
            raise SamplerError(f"Source not found: {name!r}") from exc

    # ── fast modes (recent / random), run in a worker thread ───

    def _fast_sample(
        self, req: SampleRequest, ch: Any, target: str, source_label: str | None, limit: int
    ) -> tuple[list[str], dict[str, Any], str | None]:
        if req.backend == SampleBackend.KAFKA:
            return self._kafka_fast(req, target, limit)
        where, params = ch_reader.build_where(
            source_label=source_label,
            filter_sql=req.filter,
            since=req.since,
            until=req.until,
            timestamp_field=self._cfg.timestamp_field,
        )
        if req.mode == SampleMode.RECENT:
            lines = ch_reader.read_recent(
                ch,
                target,
                limit=limit,
                where=where,
                params=params,
                timestamp_field=self._cfg.timestamp_field,
                max_execution_time=self._cfg.max_execution_time,
            )
        else:  # RANDOM
            lines = ch_reader.read_random(
                ch,
                target,
                limit=limit,
                where=where,
                params=params,
                seed=req.seed,
                max_execution_time=self._cfg.max_execution_time,
            )
        return lines, {"scanned": len(lines), "truncated": len(lines) >= limit}, None

    def _kafka_fast(
        self, req: SampleRequest, topic: str, limit: int
    ) -> tuple[list[str], dict[str, Any], str | None]:
        conf = kr.librdkafka_config(self._kafka)
        suffix = uuid.uuid4().hex[:8]
        if req.mode == SampleMode.RECENT:
            lines = kr.read_recent(conf, topic, limit=limit, group_suffix=suffix)
            # truncated follows the ONE documented meaning (SampleResult.truncated:
            # "more rows were available than returned"), same as the CH + kafka
            # random paths: a full window (>= limit) means older messages were left
            # unread, so more is available; a short read cut nothing (P3.12).
            return lines, {"read": len(lines), "truncated": len(lines) >= limit}, None
        # RANDOM over Kafka: read a bounded window from the earliest offset, then
        # sample it. There is no server-side random on a topic, so this is a fair
        # sample of the window, not of all history - say so.
        window = kr.read_recent(
            conf, topic, limit=self._cfg.kafka_max_messages, group_suffix=suffix
        )
        lines = _reservoir(window, limit, req.seed)
        note = "random over a bounded tail window, not the whole topic"
        return lines, {"window": len(window), "truncated": len(window) > limit}, note

    # ── gated modes (smart / anomaly) via logreducer ───────────

    def _reduce_sample(
        self, req: SampleRequest, ch: Any, target: str, source_label: str | None, limit: int
    ) -> tuple[list[str], dict[str, Any], str | None]:
        try:
            from logreducer import LogReducer
        except ImportError as exc:
            raise SamplerError(
                f"'{req.mode.value}' mode needs logreducer, which is not installed. "
                "Use mode=recent or mode=random, or install logreducer."
            ) from exc

        level = req.level or self._cfg.level
        lr_mode = "anomaly" if req.mode == SampleMode.ANOMALY else "hybrid"
        reducer = LogReducer(level=level, mode=lr_mode, max_memory_gb=self._cfg.max_memory_gb)
        source = self._build_reduce_source(req, ch, target, source_label)
        reduced = reducer.reduce(source)

        truncated = len(reduced) > limit
        lines = reduced[:limit]
        stats = {
            "reduced_total": len(reduced),
            "level": level,
            "logreducer_mode": lr_mode,
            "scan_cap": self._cfg.max_scan_rows,
            "truncated": truncated,
        }
        note = (
            None if not truncated else f"reduced to {len(reduced)} representatives, showing {limit}"
        )
        return lines, stats, note

    def _build_reduce_source(
        self, req: SampleRequest, ch: Any, target: str, source_label: str | None
    ) -> Any:
        suffix = uuid.uuid4().hex[:8]
        if req.backend == SampleBackend.KAFKA:
            conf = kr.librdkafka_config(self._kafka)
            return kr.build_source(
                conf, target, max_messages=self._cfg.max_scan_rows, group_suffix=suffix
            )
        from logreducer.clickhouse import ClickHouseSource

        where, params = ch_reader.build_where(
            source_label=source_label,
            filter_sql=req.filter,
            since=req.since,
            until=req.until,
            timestamp_field=self._cfg.timestamp_field,
        )
        sql = ch_reader.scan_query(target, where=where, scan_rows=self._cfg.max_scan_rows)
        # The gated path streams via logreducer's ClickHouseSource on the UNWRAPPED
        # native client (block streaming), so it bypasses the TenantScopedClient's
        # setting handling. A readonly fixed user (dfe_analyst_ro / dfe_tenant_reader)
        # would REJECT the per-query max_execution_time (not CHANGEABLE_IN_READONLY),
        # so drop it for them and lean on their profile bound. NB the native client
        # also bypasses tenant injection - unreachable today (sampler:read is not a
        # tenant action), a noted follow-up if it ever becomes one.
        settings = (
            None
            if getattr(ch, "readonly", False)
            else {"max_execution_time": self._cfg.max_execution_time}
        )
        return ClickHouseSource(
            ch_reader._raw_client(ch),
            sql,
            parameters=params or None,
            settings=settings,
        )

    # ── formatting ─────────────────────────────────────────────

    def _format(
        self,
        req: SampleRequest,
        target: str,
        limit: int,
        lines: list[str],
        stats: dict[str, Any],
        note: str | None,
    ) -> SampleResult:
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                rows.append(obj)
        return SampleResult(
            mode=req.mode,
            backend=req.backend,
            source=req.source,
            target=target,
            count=len(lines),
            requested_limit=limit,
            lines=lines,
            rows=rows,
            keys=discover_json_keys(lines),
            truncated=bool(stats.get("truncated", False)),
            stats=stats,
            note=note,
        )


def _reservoir(items: list[str], k: int, seed: int | None) -> list[str]:
    """A deterministic (seeded) random sample of up to ``k`` items."""
    import random

    if len(items) <= k:
        return items
    rng = random.Random(seed)  # noqa: S311 - sampling, not cryptography
    return rng.sample(items, k)


def _progress(task: Any, pct: int, message: str) -> None:
    if task is not None:
        try:
            task.set_progress(pct, message)
        except Exception:  # pragma: no cover - progress is best-effort
            logger.debug("sampler progress update failed", exc_info=True)
