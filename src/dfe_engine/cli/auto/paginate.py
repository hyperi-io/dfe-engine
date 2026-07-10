#  Project:      dfe-engine
#  File:         cli/auto/paginate.py
#  Purpose:      Auto-follow the engine's PaginatedResponse envelope for `list`
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Detect + auto-follow the engine pagination envelope.

The engine returns ``PaginatedResponse[T]`` (schema ref ``PaginatedResponse_*``)
with keys ``items``, ``page``, ``per_page``, ``next_page`` (the next page number,
or null at the end). For a ``list`` verb on a paginated op the CLI walks the pages
(``page=1,2,...``) accumulating ``items`` until ``next_page`` is null, the
``--limit`` total cap is hit, or ``--no-paginate`` forces a single page.

``--page-size`` maps to the ``per_page`` query parameter.
"""

from __future__ import annotations

import sys
from typing import Any

from .client import Client
from .spec import Operation


def is_paginated(op: Operation) -> bool:
    """True if the op's success response is our pagination envelope."""
    ref = op.response_ref_name or ""
    if ref.startswith("PaginatedResponse_"):
        return True
    schema = op.success_response_schema or {}
    props = schema.get("properties", {})
    return "items" in props and "next_page" in props


def _looks_like_envelope(body: Any) -> bool:
    return isinstance(body, dict) and isinstance(body.get("items"), list) and "next_page" in body


def collect(
    client: Client,
    op: Operation,
    *,
    path_args: dict[str, Any],
    query_args: dict[str, Any],
    page_size: int | None = None,
    limit: int | None = None,
    no_paginate: bool = False,
) -> list[Any]:
    """Return the accumulated ``items`` across pages (respecting limit/no-paginate)."""
    query = dict(query_args)
    if page_size is not None:
        query["per_page"] = page_size

    items: list[Any] = []
    page = query.get("page", 1) or 1
    while True:
        override = {"page": page}
        body = client.call_json(
            op,
            path_args=path_args,
            query_args=query,
            query_override=override,
        )
        if not _looks_like_envelope(body):
            # Not actually paginated at runtime - return whatever came back.
            if isinstance(body, list):
                return body[:limit] if limit is not None else body
            return body
        items.extend(body["items"])
        if limit is not None and len(items) >= limit:
            return items[:limit]
        if no_paginate:
            return items[:limit] if limit is not None else items
        next_page = body.get("next_page")
        if not next_page:
            break
        # Guard a misbehaving server: next_page must strictly advance past the page
        # we just fetched. If it loops or repeats we would spin forever (no --limit
        # cap), so stop and warn rather than hang.
        if not (isinstance(next_page, int) and next_page > page):
            sys.stderr.write(
                f"warning: pagination stopped - next_page ({next_page!r}) did not "
                f"advance past page {page}.\n"
            )
            break
        page = next_page
    return items[:limit] if limit is not None else items
