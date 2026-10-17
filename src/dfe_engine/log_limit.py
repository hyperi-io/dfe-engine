#  Project:      dfe-engine
#  File:         log_limit.py
#  Purpose:      One log line a period for an event every request can repeat
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A log line any caller can repeat, written once a period through scalo's rate limit.

scalo-py's ``RateLimitFilter`` holds back a repeat of the same message inside its
period and names how many it held when the next one goes out. scalo applies it to
every line of a sink, which would also thin out audit events, so :class:`LineLimit`
puts one filter in front of one event instead. A counter beside it keeps the number
exact.
"""

from typing import Any

from scalo.logger import logger
from scalo.logger.filters import RateLimitFilter


class LineLimit:
    """At most one warning line a period for each event, the next naming how many were held.

    Not thread-safe: a caller on the event loop needs no lock.
    """

    def __init__(self, period_seconds: int) -> None:
        """Hold back a repeat of an event inside ``period_seconds`` of the last one written."""
        self._filter = RateLimitFilter(period_sec=period_seconds)

    def warning(self, event: str, **fields: Any) -> None:
        """Log ``event`` at warning unless the same event went out inside the period.

        Args:
            event: The log line's message, the key repeats are held back on.
            **fields: The line's structured fields.
        """
        record = {"name": __name__, "level": logger.level("WARNING"), "message": event}
        if self._filter(record):
            logger.warning(record["message"], **fields)
