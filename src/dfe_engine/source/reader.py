#  Project:      dfe-engine
#  File:         source/reader.py
#  Purpose:      The read surface the routing compilers take of the source definitions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The two reads a compiler makes of the source definitions.

:class:`~dfe_engine.source.registry.SourceRegistry` serves them, and so does any
reader that holds the same definitions.
"""

from collections.abc import Collection
from typing import Protocol

from dfe_engine.source.models import Source


class SourceReader(Protocol):
    """Reads source definitions by name, or all of them by state."""

    def get_source(self, source_name: str, /) -> Source:
        """Return the source named *source_name*, raising SourceNotFoundError when there is none."""
        ...

    def get_all_sources(
        self, enabled_only: bool = False, *, states: Collection[str] | None = None
    ) -> list[Source]:
        """Return every source, or only those in *states*, or only enabled ones."""
        ...
