#  Project:      dfe-engine
#  File:         transport.py
#  Purpose:      The two ways one source's stages hand records to the next
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Bus or direct - what a source records, and what a deployment provides.

A source records only which of the two carries it. Which bus (Kafka today) and
which direct protocol (gRPC) are deployment facts, held in ``settings.transport``,
so a second bus provider joins behind the same ``bus`` value without a change to
the source model.

Its own module because both ends need the name: ``dfe_engine.settings`` holds the
deployment fact and ``dfe_engine.source.models`` holds the per-source choice, and
the source package reaches the API package through pagination.
"""

from __future__ import annotations

from typing import Literal, get_args

SourceTransport = Literal["bus", "direct"]
"""bus: a broker holds records between stages. direct: point to point, no store."""

TRANSPORTS: frozenset[str] = frozenset(get_args(SourceTransport))
"""Every transport name, for validating manifest and config values against one list."""
