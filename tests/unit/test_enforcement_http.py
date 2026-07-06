#  Project:      dfe-engine
#  File:         tests/unit/test_enforcement_http.py
#  Purpose:      Guard - outbound HTTP goes through the scalo.http seam, not raw httpx
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Enforcement guard for the "outbound HTTP routes through scalo.http" rule.

Every outbound HTTP call in dfe-engine uses ``scalo.http`` (HttpClient /
AsyncHttpClient) so it gets ONE retry / backoff / breaker / traceparent policy -
never a raw ``httpx.Client`` / ``httpx.AsyncClient``. This is the HTTP twin of the
ClickHouse enforcement guard: it stops the sprawl (transforms.py once opened its
own ``httpx.AsyncClient`` with zero retry) from creeping back.

httpx EXCEPTION types (``httpx.HTTPStatusError``, ``httpx.ConnectError``) and
value types (``httpx.Response``) ARE allowed - scalo.http raises and returns them,
so a caller must be able to catch/annotate them. Only the raw CLIENT classes are
banned.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "dfe_engine"

# The raw client classes - constructing or annotating one bypasses the scalo seam.
# Exception/value types (httpx.HTTPStatusError, httpx.Response, ...) do NOT match.
_RAW_CLIENT_RE = re.compile(r"\bhttpx\.(?:Async)?Client\b")


def _py_files() -> list[Path]:
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_raw_httpx_client_outside_scalo() -> None:
    """dfe code must reach for scalo.http.AsyncHttpClient / HttpClient, never httpx's."""
    violations: list[str] = []
    for path in _py_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue  # a comment naming the class is not a real use
            if _RAW_CLIENT_RE.search(line):
                violations.append(f"{path.relative_to(_SRC)}:{lineno}: {line.strip()}")
    assert not violations, (
        "Raw httpx.Client / httpx.AsyncClient - route outbound HTTP through "
        "scalo.http.HttpClient / AsyncHttpClient (retry + backoff + breaker):\n"
        + "\n".join(violations)
    )
