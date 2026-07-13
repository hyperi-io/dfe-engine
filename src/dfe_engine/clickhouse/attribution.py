#  Project:      dfe-engine
#  File:         clickhouse/attribution.py
#  Purpose:      Per-query attribution tags -> ClickHouse log_comment
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Query attribution - every ClickHouse query carries a ``log_comment`` tag payload.

A :class:`DfeQueryTags` snapshot lives in a ``ContextVar``, set via
:func:`tags_context` at each API / hunt / CLI entrypoint. The canonical execution
facade serialises it into ``settings["log_comment"]`` on EVERY query, so
``system.query_log.log_comment`` records who / what / why per query (tenant, user,
feature, kind). Pattern adapted from PostHog (MIT) - see THIRD-PARTY-NOTICES.

This is the SSoT the hunt-cost / attribution views read (a ``query_log_archive``
materialised view over ``system.query_log`` unblocks the cost leaderboard - P2).
Attribution is best-effort: an unset context yields the base tags (service only),
never an error.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class DfeQueryTags:
    """Attribution for one logical unit of work, stamped onto its CH queries.

    Every field is optional except ``service`` so an entrypoint sets only what it
    knows. Frozen: updates are copy-on-write (``replace``), so a nested
    ``tags_context`` never mutates an outer scope's snapshot.
    """

    service: str = "dfe-engine"
    tenant_id: str | None = None  # org_ids / tenant axis
    user: str | None = None  # the acting principal (account name / api-key id)
    feature: str | None = None  # the API / subsystem issuing the query
    kind: str | None = None  # query class (read / ddl / insert / reconcile / ...)
    id: str | None = None  # correlating id (hunt_id, request_id, ...)
    source: str | None = None  # source_file:line, when captured
    trace_id: str | None = None  # W3C trace id, when propagated

    def to_json(self) -> str:
        """Compact JSON of the set fields - the ``log_comment`` payload."""
        return json.dumps(
            {k: v for k, v in asdict(self).items() if v is not None},
            separators=(",", ":"),
            sort_keys=True,
        )

    def brief(self) -> str:
        """Short human breadcrumb for a leading ``/* ... */`` SQL comment."""
        bits = [b for b in (self.feature, self.kind, self.id) if b]
        return " ".join(bits)


_BASE = DfeQueryTags()
_TAGS: contextvars.ContextVar[DfeQueryTags] = contextvars.ContextVar("dfe_ch_tags", default=_BASE)


def current_tags() -> DfeQueryTags:
    """The attribution tags for the current context (base tags if none set)."""
    return _TAGS.get()


def tag_queries(**fields: Any) -> None:
    """Merge ``fields`` into the ambient tags (copy-on-write) for this context.

    Use at an entrypoint that owns the whole request; prefer :func:`tags_context`
    where the scope is bounded so the tags are restored on exit.
    """
    _TAGS.set(replace(_TAGS.get(), **fields))


@contextmanager
def tags_context(**fields: Any) -> Iterator[None]:
    """Scope attribution ``fields`` to a block, restoring the prior tags on exit.

    Restores even on exception. Nested contexts stack (each restores its own
    snapshot). A bare ``threading.Thread`` that does not copy the context starts
    from the base tags - copy the context (or re-tag) across a manual thread hop.
    """
    snapshot = _TAGS.get()
    if fields:
        _TAGS.set(replace(snapshot, **fields))
    try:
        yield
    finally:
        _TAGS.set(snapshot)


def merge_log_comment(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Return a settings dict with the current tags' ``log_comment`` merged in.

    A caller-supplied ``log_comment`` is preserved (``setdefault``); otherwise the
    ambient :class:`DfeQueryTags` JSON is used. Always returns a fresh dict so the
    caller's is never mutated.
    """
    merged = dict(settings or {})
    merged.setdefault("log_comment", current_tags().to_json())
    return merged
