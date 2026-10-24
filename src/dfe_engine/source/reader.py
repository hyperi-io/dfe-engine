#  Project:      dfe-engine
#  File:         source/reader.py
#  Purpose:      The read surface the routing compilers take of the source definitions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The reads a compiler makes of the source definitions.

:class:`~dfe_engine.source.registry.SourceRegistry` serves them, and so does any
reader that holds the same definitions. The ingest rules live beside the sources,
so the reader that holds them serves those too.
"""

from collections.abc import Collection
from typing import TYPE_CHECKING, Protocol

from dfe_engine.source.models import Source

if TYPE_CHECKING:
    from dfe_engine.ingest.models import IngestRules


class SourceReader(Protocol):
    """Reads source definitions by name, or all of them by state, and the ingest rules beside them."""

    def ingest_rules(self) -> IngestRules:
        """Return the rule order, compatibility profiles and default bindings the sources compile with."""
        ...

    def get_source(self, source_name: str, /) -> Source:
        """Return the source named *source_name*, raising SourceNotFoundError when there is none."""
        ...

    def get_all_sources(
        self, enabled_only: bool = False, *, states: Collection[str] | None = None
    ) -> list[Source]:
        """Return every source, or only those in *states*, or only enabled ones."""
        ...
