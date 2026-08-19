#  Project:      dfe-engine
#  File:         store/__init__.py
#  Purpose:      Generic document store for engine control-plane state
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Reusable document store layer (FerretDB / mongo wire protocol)."""

from __future__ import annotations

from dfe_engine.store.documents import DocumentCollection, DocumentStore

__all__ = ["DocumentCollection", "DocumentStore"]
