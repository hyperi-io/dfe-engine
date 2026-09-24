#  Project:      dfe-engine
#  File:         api/body_limits.py
#  Purpose:      Refuse an oversized request body with 413 before it is buffered
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Bounded reads of a request body.

A route that declares a body model has FastAPI read the whole body before the
handler runs, so a route that caps its body reads the stream itself: the
declared Content-Length is refused first, then a running total as chunks arrive.
"""

from fastapi import HTTPException, Request, status


def too_large(code: str, message: str) -> HTTPException:
    """The 413 a capped route answers with."""
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={"code": code, "message": message},
    )


def refuse_declared_oversize(request: Request, *, limit: int, code: str, message: str) -> None:
    """Refuse a body whose declared Content-Length exceeds ``limit``, unread.

    A missing or malformed header is not a refusal: the read that follows still
    counts what actually arrives.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        declared = int(raw)
    except ValueError:
        return
    if declared > limit:
        raise too_large(code, message)


async def read_body_capped(request: Request, *, limit: int, code: str, message: str) -> bytes:
    """The request body, refused as soon as more than ``limit`` bytes have arrived."""
    refuse_declared_oversize(request, limit=limit, code=code, message=message)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise too_large(code, message)
        chunks.append(chunk)
    return b"".join(chunks)
