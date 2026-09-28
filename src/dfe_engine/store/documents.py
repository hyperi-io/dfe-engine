#  Project:      dfe-engine
#  File:         store/documents.py
#  Purpose:      Generic sync document store over the mongo wire protocol
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Reusable document store for engine control-plane state (mongo wire protocol).

The engine is a control plane for a small number of operators, so this layer is
deliberately synchronous (PyMongo's ``MongoClient``): it runs inside FastAPI's
threadpool like the other stores and carries no async machinery. It is the shared
foundation for per-user / per-engine documents -- local accounts first, then user
preferences, teams and sharing.

One client, one database (default ``dfe_engine``), typed pydantic collections keyed
on a unique field.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection as MongoCollection

T = TypeVar("T", bound=BaseModel)

type OnInvalid = Callable[[dict, ValidationError], None]
"""Called with a stored document no model can be made from, and the error."""


class DocuStore:
    """A sync connection to one document-store database, shared across consumers.

    ``MongoClient`` connects lazily, so constructing this never blocks; the first
    real operation (or an explicit :meth:`ping`) is what surfaces an unreachable
    server. Hold ONE instance per process (built in the app lifespan) and hand out
    collections from it; call :meth:`close` on shutdown.
    """

    def __init__(self, uri: str, database: str = "dfe_engine") -> None:
        self._client: MongoClient = MongoClient(uri, appname="dfe-engine")
        self._db = self._client[database]

    def collection(self, name: str) -> MongoCollection:
        """Raw pymongo collection handle (for consumers needing native queries)."""
        return self._db[name]

    def typed(
        self,
        name: str,
        model: type[T],
        *,
        key: str,
        on_invalid: OnInvalid | None = None,
    ) -> DocumentCollection[T]:
        """A typed pydantic collection on ``name``, keyed on the unique field ``key``.

        ``on_invalid`` opts in to skipping a stored document no ``model`` can be made
        from; without it such a document raises ``ValidationError``.
        """
        return DocumentCollection(self._db[name], model, key=key, on_invalid=on_invalid)

    def ping(self) -> None:
        """Round-trip the server; raises on failure. Use for a readiness check."""
        self._client.admin.command("ping")

    def drop_database(self, name: str) -> None:
        """Drop a database by name. For cleanup / test teardown only."""
        self._client.drop_database(name)

    def close(self) -> None:
        """Close the client and its pooled connections."""
        self._client.close()


class DocumentCollection(Generic[T]):
    """Typed CRUD over one collection, keyed on a unique application field.

    The key field is stored in the document AND carries a unique index; mongo's
    ``_id`` is left untouched and stripped on read. Any consumer that persists one
    pydantic model per document reuses this -- accounts is the first, preferences
    and teams follow.

    A document no ``model`` can be made from raises ``ValidationError`` on read,
    unless ``on_invalid`` is given: then it is called with the document and the
    error, and the document reads as absent.
    """

    def __init__(
        self,
        collection: MongoCollection,
        model: type[T],
        *,
        key: str,
        on_invalid: OnInvalid | None = None,
    ) -> None:
        self._c = collection
        self._model = model
        self._key = key
        self._on_invalid = on_invalid
        # Idempotent; enforces one document per key value.
        self._c.create_index([(key, ASCENDING)], unique=True)

    def get(self, key_value: str) -> T | None:
        """Return the document with ``key == key_value``, or None."""
        return self._load(self._c.find_one({self._key: key_value}))

    def list(self) -> list[T]:
        """Return all documents, sorted by the key field."""
        return [m for m in (self._load(d) for d in self._c.find().sort(self._key, ASCENDING)) if m]

    def put(self, key_value: str, model: T) -> None:
        """Upsert ``model`` under ``key_value`` (create or full replace)."""
        self._c.replace_one({self._key: key_value}, self._dump(model), upsert=True)

    def delete(self, key_value: str) -> bool:
        """Delete the document with ``key == key_value``; True if one was removed."""
        return self._c.delete_one({self._key: key_value}).deleted_count > 0

    def exists(self, key_value: str) -> bool:
        """Whether a document with ``key == key_value`` exists."""
        return self._c.count_documents({self._key: key_value}, limit=1) > 0

    def _load(self, doc: dict | None) -> T | None:
        if doc is None:
            return None
        doc.pop("_id", None)
        if self._on_invalid is None:
            return self._model.model_validate(doc)
        try:
            return self._model.model_validate(doc)
        except ValidationError as exc:
            self._on_invalid(doc, exc)
            return None

    def _dump(self, model: T) -> dict:
        return model.model_dump()
