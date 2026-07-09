#  Project:      dfe-engine
#  File:         __init__.py
#  Purpose:      Repository package - scope-aligned small-object store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Scope-aligned small-object store for dfe-ui (preferences, JSON docs, files)."""

from dfe_engine.repository.store import ConflictError, RepositoryStore, json_merge_patch

__all__ = ["ConflictError", "RepositoryStore", "json_merge_patch"]
