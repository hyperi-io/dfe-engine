#  Project:      dfe-engine
#  File:         synthetic_data/autostart.py
#  Purpose:      Standing demo streams - keep configured packs streaming for the pod's life
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Standing synthetic data streams.

The sales-demo posture: a deployment names reference packs and a receiver URL
(``synthetic_data.autostart_*``) and the engine keeps a continuous live-tail
stream running for each pack - restarted in bounded segments so the duration
ceiling holds, retried with a backoff when the receiver is away, cancelled at
shutdown. Default configuration is empty, so nothing runs unless an operator
turns it on.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.synthetic_data.models import StreamRequest, SyntheticDataError

if TYPE_CHECKING:
    from dfe_engine.settings import SyntheticDataSettings
    from dfe_engine.synthetic_data.service import SyntheticDataService

_RETRY_SECONDS = 30.0
_SEGMENT_SECONDS = 3600.0


async def run_standing_stream(
    service: SyntheticDataService,
    request: StreamRequest,
    *,
    retry_seconds: float = _RETRY_SECONDS,
) -> None:
    """Stream one pack forever in bounded segments; degrade and retry on errors."""
    while True:
        try:
            summary = await service.run_stream(request)
            logger.info("standing synthetic stream segment complete", **summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "standing synthetic stream segment failed - retrying",
                schema=request.schema_ref,
                error=str(exc),
            )
            await asyncio.sleep(retry_seconds)


def start_autostart(
    service: SyntheticDataService, settings: SyntheticDataSettings
) -> list[asyncio.Task]:
    """Launch a standing stream task per configured pack (none when unset)."""
    refs = [ref.strip() for ref in settings.autostart_schemas.split(",") if ref.strip()]
    if not refs:
        return []
    if not settings.autostart_receiver_url:
        logger.warning(
            "synthetic autostart schemas set but no receiver URL - not starting",
            schemas=settings.autostart_schemas,
        )
        return []

    segment = min(_SEGMENT_SECONDS, settings.max_stream_seconds)
    tasks: list[asyncio.Task] = []
    for ref in refs:
        request = StreamRequest(
            schema_ref=ref,
            receiver_url=settings.autostart_receiver_url,
            rate_eps=settings.autostart_rate_eps,
            duration_s=segment,
            seed=settings.autostart_seed,
        )
        try:
            service.validate_stream(request)
        except SyntheticDataError as exc:
            logger.warning("synthetic autostart pack skipped", schema=ref, error=str(exc))
            continue
        tasks.append(
            asyncio.get_running_loop().create_task(
                run_standing_stream(service, request), name=f"synthetic-autostart:{ref}"
            )
        )
        logger.info(
            "standing synthetic stream started",
            schema=ref,
            receiver_url=settings.autostart_receiver_url,
            rate_eps=settings.autostart_rate_eps,
        )
    return tasks
