#  Project:      dfe-engine
#  File:         auth/store_backend.py
#  Purpose:      Resolve the local users/groups store backend (document store when available)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Resolve one central backend for local users AND groups.

``auth.store_backend`` is the single config-cascade knob (SSoT). ``auto`` (the
default) uses the document store when a reachable URI is configured and falls
back to yaml files otherwise, so every deployment that ships a document store
gets it automatically while a bare local engine or the hermetic tests keep
working with no database. ``document`` forces it (and fails loudly if
unreachable -- an explicit opt-in); ``yaml`` forces the file store.

gitcrud is deliberately NOT a store option here: only the break-glass admin is
additionally git-persisted (see :mod:`dfe_engine.auth.account_durability`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scalo.logger import logger

if TYPE_CHECKING:
    from dfe_engine.store.documents import DocuStore


def resolve_store_backend(requested: str, uri: str, database: str) -> tuple[str, DocuStore | None]:
    """Return the effective backend ('document' | 'yaml') and a DocuStore if document.

    The DocuStore (when returned) is shared by the account and group stores, so
    the caller owns its lifecycle (close on shutdown). ``MongoClient`` connects
    lazily; :meth:`DocuStore.ping` is the actual reachability check.
    """
    from dfe_engine.store.documents import DocuStore

    backend = (requested or "auto").lower()

    if backend == "yaml":
        return "yaml", None

    if backend == "document":
        store = DocuStore(uri, database)
        store.ping()  # explicit opt-in: fail loudly if the operator's document store is down
        logger.info("Users/groups store backend: document (forced)")
        return "document", store

    # auto: document store when a reachable URI is configured, else yaml.
    if not uri:
        logger.info("Users/groups store backend: yaml (auto -- no document store URI configured)")
        return "yaml", None
    store = DocuStore(uri, database)
    try:
        store.ping()
    except Exception as exc:  # unreachable document store must never break startup
        logger.warning(
            "Users/groups store backend: yaml (auto -- document store URI configured but unreachable)",
            error=str(exc),
        )
        store.close()
        return "yaml", None
    logger.info("Users/groups store backend: document (auto -- reachable)")
    return "document", store
