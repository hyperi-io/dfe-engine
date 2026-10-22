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

Several engine replicas write the same documents, so every write here is safe to
race. Each document carries ``_rev``, which every write moves by one, and
:meth:`DocumentCollection.mutate` replaces a document only at the revision it read.
Every write also moves its collection's counter in ``store_revisions``, so a replica
can tell that another changed a collection without reading it. DocumentDB fails the
losing side of a write race with code 11000, or with 112 when it gives up on a lock,
upserts included, so each write retries those a bounded number of times and reports a
race it never won as :class:`DocumentConflictError`.
"""

import builtins
import functools
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection as MongoCollection
from pymongo.errors import DuplicateKeyError, OperationFailure

from dfe_engine.backoff import jittered_sleep

type OnInvalid = Callable[[dict, ValidationError], None]
"""Called with a stored document no model can be made from, and the error."""

type Prepare = Callable[[dict], dict]
"""Called with each stored document before validation; returns the document to validate."""

REV = "_rev"
"""The revision every document carries, moved by one on each write."""

REVISIONS_COLLECTION = "store_revisions"
"""One document per collection, ``{_id: <collection>, rev: <writes>}``."""

DEFAULT_ATTEMPTS = 5
"""How many times a write is tried before a race it keeps losing is a conflict."""

SERVER_SELECTION_TIMEOUT_SECONDS = 5.0
"""How long a call waits for a store it cannot reach, where nothing passes one in.

The engine passes ``auth.accounts_store.server_selection_timeout_seconds``, which
defaults to the same five seconds, so this is the last resort for direct construction.
"""

SOCKET_TIMEOUT_SECONDS = 30.0
"""How long a call waits on a store that stopped answering mid-call, where nothing passes one in.

The engine passes ``auth.accounts_store.socket_timeout_seconds``, which defaults to the
same thirty seconds, so this is the last resort for direct construction.
"""

# DuplicateKey, and DocumentDB's WriteConflict "Could not acquire lock for operation due to deadlock".
_CONFLICT_CODES = frozenset({11000, 112})
# InternalError, DocumentDB's answer to a createIndexes it could not finish, and WriteConflict.
_INDEX_RETRY_CODES = frozenset({1, 112})
# The first wait between attempts, doubled after each, up to the cap. Seconds.
_BACKOFF_FIRST = 0.01
_BACKOFF_CAP = 0.2


class DocumentConflictError(RuntimeError):
    """A write lost to concurrent writers on every attempt it was allowed."""


def _backoff(attempt: int) -> None:
    """Wait before retry number *attempt*, jittered so racing replicas spread out."""
    jittered_sleep(min(_BACKOFF_CAP, _BACKOFF_FIRST * 2 ** (attempt - 1)))


def _retrying[R](op: Callable[[], R], *, what: str) -> R:
    """Run *op*, retrying the codes DocumentDB fails a lost race with.

    Raises:
        DocumentConflictError: Every attempt lost a race.
        OperationFailure: The store refused the write for any other reason.
    """
    for attempt in range(DEFAULT_ATTEMPTS):
        if attempt:
            _backoff(attempt)
        try:
            return op()
        except OperationFailure as exc:
            if exc.code not in _CONFLICT_CODES:
                raise
    raise DocumentConflictError(f"{what} lost to concurrent writers {DEFAULT_ATTEMPTS} times")


class _Revisions:
    """The per-collection write counters in ``store_revisions``, keyed on ``_id``."""

    def __init__(self, collection: MongoCollection) -> None:
        self._c = collection

    def bump(self, name: str) -> None:
        """Count one write to collection *name*."""
        _retrying(
            lambda: self._c.update_one({"_id": name}, {"$inc": {"rev": 1}}, upsert=True),
            what=f"the {name} revision",
        )

    def get(self, name: str) -> int:
        """The writes collection *name* has taken, 0 before its first."""
        doc = self._c.find_one({"_id": name})
        return int(doc["rev"]) if doc is not None else 0


class DocuStore:
    """A sync connection to one document-store database, shared across consumers.

    ``MongoClient`` connects lazily, so constructing this never blocks; the first
    real operation (or an explicit :meth:`ping`) is what surfaces an unreachable
    server. Hold ONE instance per process (built in the app lifespan) and hand out
    collections from it; call :meth:`close` on shutdown.

    A call that finds no store it can reach raises ``ServerSelectionTimeoutError``
    after ``server_selection_timeout`` seconds, rather than pymongo's thirty. One that
    reached the store and gets no answer raises ``NetworkTimeout`` after
    ``socket_timeout`` seconds, where pymongo would wait for ever.
    """

    def __init__(
        self,
        uri: str,
        database: str = "dfe_engine",
        *,
        server_selection_timeout: float = SERVER_SELECTION_TIMEOUT_SECONDS,
        socket_timeout: float = SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        self._client: MongoClient = MongoClient(
            uri,
            appname="dfe-engine",
            serverSelectionTimeoutMS=round(server_selection_timeout * 1000),
            socketTimeoutMS=round(socket_timeout * 1000),
        )
        self._db = self._client[database]
        self._revisions = _Revisions(self._db[REVISIONS_COLLECTION])

    def collection(self, name: str) -> MongoCollection:
        """Raw pymongo collection handle (for consumers needing native queries)."""
        return self._db[name]

    def typed[M: BaseModel](
        self,
        name: str,
        model: type[M],
        *,
        key: str,
        unique: Iterable[str] = (),
        on_invalid: OnInvalid | None = None,
        prepare: Prepare | None = None,
    ) -> DocumentCollection[M]:
        """A typed pydantic collection on ``name``, keyed on the unique field ``key``.

        ``unique`` names further fields no two documents may share. ``on_invalid``
        opts in to skipping a stored document no ``model`` can be made from, and
        without it such a document raises ``ValidationError``. ``prepare`` rewrites
        each stored document before it is validated.
        """
        return DocumentCollection(
            self._db[name],
            model,
            key=key,
            unique=unique,
            revisions=self._revisions,
            on_invalid=on_invalid,
            prepare=prepare,
        )

    def claims(self, name: str) -> Claims:
        """Exclusive claims on string keys, held in collection ``name``."""
        return Claims(self.typed(name, Claim, key="key"))

    def revision(self, name: str) -> int:
        """How many writes collection *name* has taken, 0 before its first.

        A reader holding something built from the collection rebuilds it when this
        moves, whichever replica made the write.
        """
        return self._revisions.get(name)

    def ping(self) -> None:
        """Round-trip the server; raises on failure. Use for a readiness check."""
        self._client.admin.command("ping")

    def drop_database(self, name: str) -> None:
        """Drop a database by name. For cleanup / test teardown only."""
        self._client.drop_database(name)

    def close(self) -> None:
        """Close the client and its pooled connections."""
        self._client.close()


class DocumentCollection[T: BaseModel]:
    """Typed CRUD over one collection, keyed on a unique application field.

    The key field is stored in the document AND carries a unique index; mongo's
    ``_id`` is left untouched and stripped on read, as is ``_rev``. Any consumer that
    persists one pydantic model per document reuses this -- accounts is the first,
    preferences and teams follow.

    A document no ``model`` can be made from raises ``ValidationError`` on read,
    unless ``on_invalid`` is given: then it is called with the document and the
    error, and the document reads as absent. ``prepare``, when given, rewrites each
    document before it is validated.
    """

    def __init__(
        self,
        collection: MongoCollection,
        model: type[T],
        *,
        key: str,
        revisions: _Revisions,
        unique: Iterable[str] = (),
        on_invalid: OnInvalid | None = None,
        prepare: Prepare | None = None,
    ) -> None:
        self._c = collection
        self._model = model
        self._key = key
        self._revisions = revisions
        self._on_invalid = on_invalid
        self._prepare = prepare
        for field in (key, *unique):
            self._index(field)

    def get(self, key_value: str) -> T | None:
        """Return the document with ``key == key_value``, or None."""
        return self._load(self._c.find_one({self._key: key_value}))

    def get_by(self, field: str, value: str) -> T | None:
        """Return the document whose ``field`` is ``value``, one of the ``unique`` fields, or None."""
        return self._load(self._c.find_one({field: value}))

    def list_by(self, field: str, value: str) -> builtins.list[T]:
        """Return every document whose ``field`` is ``value``, sorted by the key field."""
        found = self._c.find({field: value}).sort(self._key, ASCENDING)
        return [m for m in (self._load(d) for d in found) if m]

    def list(self) -> builtins.list[T]:
        """Return all documents, sorted by the key field."""
        return [m for m in (self._load(d) for d in self._c.find().sort(self._key, ASCENDING)) if m]

    def put(self, key_value: str, model: T) -> None:
        """Create or overwrite ``key_value`` with every field ``model`` carries.

        The last of two concurrent puts wins. A field the stored document carries and
        the model does not is left as it was.

        Raises:
            DocumentConflictError: Every attempt lost a race.
        """
        update = {"$set": self._dump(model), "$inc": {REV: 1}}
        _retrying(
            lambda: self._c.update_one({self._key: key_value}, update, upsert=True),
            what=f"{self._c.name} put",
        )
        self._revisions.bump(self._c.name)

    def insert(self, model: T) -> bool:
        """Store ``model`` as a new document; False when its key value is already taken.

        The unique index on the key decides, so of two clients inserting one key at
        once exactly one succeeds. A value taken in a field ``unique`` names refuses
        the insert the same way.

        Raises:
            DocumentConflictError: Every attempt lost a race without an answer.
        """
        document = self._dump(model) | {REV: 1}

        def once() -> bool:
            try:
                # A copy, because pymongo writes the generated _id into what it sends.
                self._c.insert_one(dict(document))
            except DuplicateKeyError:
                return False
            return True

        inserted = _retrying(once, what=f"{self._c.name} insert")
        if inserted:
            self._revisions.bump(self._c.name)
        return inserted

    def mutate(
        self, key_value: str, change: Callable[[T], T], *, attempts: int = DEFAULT_ATTEMPTS
    ) -> T:
        """Replace ``key_value`` with ``change`` of what is stored, unless another write landed first.

        Reads the document and its ``_rev``, applies ``change``, and replaces it only
        while it is still that document at the ``_rev`` read, so one deleted and made
        again since is never overwritten. When another write got there first it
        reads again and reapplies, so ``change`` may run more than once and must judge
        only the model it is given. A domain check that raises inside ``change``
        writes nothing. A ``change`` that alters nothing writes nothing.

        Args:
            key_value: The document's key.
            change: Returns the new model from the stored one. Must keep the key.
            attempts: Reads and conditional writes before giving up.

        Returns:
            The model as stored.

        Raises:
            KeyError: No document holds ``key_value``.
            ValueError: ``change`` altered the key, or ``attempts`` is below 1.
            DocumentConflictError: Another write landed between the read and the
                write on every attempt.
        """
        if attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {attempts}")
        for attempt in range(attempts):
            if attempt:
                _backoff(attempt)
            stored = self._c.find_one({self._key: key_value})
            if stored is None:
                raise KeyError(key_value)
            # A document deleted and made again restarts at revision 1, so _id tells it apart.
            identity = stored["_id"]
            rev = stored.get(REV)
            current = self._load(stored)
            if current is None:
                raise KeyError(key_value)
            before = self._dump(current)
            updated = change(current)
            after = self._dump(updated)
            if after == before:
                return updated
            if after.get(self._key) != key_value:
                raise ValueError(f"mutate may not change the key {self._key!r} of {key_value!r}")
            # A document written outside this layer carries no _rev until its first write here.
            expected = {REV: rev} if rev is not None else {REV: {"$exists": False}}
            try:
                result = self._c.replace_one(
                    {"_id": identity, self._key: key_value, **expected},
                    after | {REV: (rev or 0) + 1},
                )
            except OperationFailure as exc:
                if exc.code not in _CONFLICT_CODES:
                    raise
                continue
            if result.matched_count:
                self._revisions.bump(self._c.name)
                return updated
        raise DocumentConflictError(
            f"{self._c.name}/{key_value} changed under {attempts} attempts to mutate it"
        )

    def delete(self, key_value: str, *, only_if: Mapping[str, Any] | None = None) -> bool:
        """Delete the document with ``key == key_value``, and say whether one was removed.

        ``only_if`` names further field values the document must hold to be removed.

        Raises:
            DocumentConflictError: Every attempt lost a race.
        """
        query = {**(only_if or {}), self._key: key_value}
        deleted = _retrying(
            lambda: self._c.delete_one(query).deleted_count > 0, what=f"{self._c.name} delete"
        )
        if deleted:
            self._revisions.bump(self._c.name)
        return deleted

    def exists(self, key_value: str) -> bool:
        """Whether a document with ``key == key_value`` exists."""
        return self._c.count_documents({self._key: key_value}, limit=1) > 0

    def _index(self, field: str) -> None:
        """Make the unique index on *field*, which every replica asks for as it boots.

        Idempotent. A failure DocumentDB gives a busy createIndexes is retried, bounded.

        Raises:
            OperationFailure: The store refused the index, or failed it on every attempt.
        """
        for attempt in range(DEFAULT_ATTEMPTS):
            if attempt:
                _backoff(attempt)
            try:
                self._c.create_index([(field, ASCENDING)], unique=True)
                return
            except OperationFailure as exc:
                if exc.code not in _INDEX_RETRY_CODES or attempt == DEFAULT_ATTEMPTS - 1:
                    raise

    def _load(self, doc: dict | None) -> T | None:
        if doc is None:
            return None
        doc.pop("_id", None)
        doc.pop(REV, None)
        if self._prepare is not None:
            doc = self._prepare(doc)
        if self._on_invalid is None:
            return self._model.model_validate(doc)
        try:
            return self._model.model_validate(doc)
        except ValidationError as exc:
            self._on_invalid(doc, exc)
            return None

    def _dump(self, model: T) -> dict:
        return model.model_dump()


class Claim(BaseModel):
    """One claimed key, who holds it, and when they last claimed it.

    Attributes:
        key: The claimed key.
        owner: Who holds it.
        claimed_at: When the owner last claimed it, ISO 8601 UTC.
        subject: What the key is about, for listing every claim on one thing.
    """

    key: str
    owner: str
    claimed_at: str = ""
    subject: str = ""


class Claims:
    """Exclusive claims on string keys: of any number of claimers, exactly one holds a key.

    A claim is a document keyed and unique-indexed on the key, so the store's unique
    index, not a prior lookup, decides who holds it. An owner claiming a key it
    already holds succeeds and stamps it fresh, so a retried claim does not lose to
    itself and a claim still in use never reads as old.

    A claim is made before the record that needs it is written, so an owner that dies
    between the two leaves a claim nothing holds. :meth:`release_if_orphaned` frees one.
    """

    def __init__(self, collection: DocumentCollection[Claim]) -> None:
        self._c = collection

    def claim(self, key: str, owner: str, *, subject: str = "") -> bool:
        """Claim *key* for *owner*. True when *owner* holds it now, False when another does.

        Raises:
            DocumentConflictError: The key was claimed and released under every attempt.
        """
        for _ in range(DEFAULT_ATTEMPTS):
            if self._c.insert(Claim(key=key, owner=owner, claimed_at=_now(), subject=subject)):
                return True
            try:
                held = self._c.mutate(key, functools.partial(_renewed, owner))
            except KeyError:
                continue
            return held.owner == owner
        raise DocumentConflictError(f"{key} was claimed and released {DEFAULT_ATTEMPTS} times")

    def holder(self, key: str) -> str | None:
        """The owner holding *key*, or None when nobody does."""
        held = self._c.get(key)
        return held.owner if held is not None else None

    def about(self, subject: str) -> builtins.list[Claim]:
        """Every claim made with *subject*."""
        return self._c.list_by("subject", subject)

    def owned_by(self, owner: str) -> builtins.list[Claim]:
        """Every claim *owner* holds."""
        return self._c.list_by("owner", owner)

    def release(self, key: str, owner: str) -> bool:
        """Release *key* when *owner* holds it. True when it was released."""
        return self._c.delete(key, only_if={"owner": owner})

    def release_if_orphaned(self, claim: Claim, *, held: bool, grace_seconds: float) -> bool:
        """Release *claim* when its owner's record does not hold it and it is older than the grace.

        The grace leaves alone a claim whose owner is still writing the record that will
        hold it. The release matches the claim as read, so one renewed since is kept.

        Args:
            claim: A claim as read.
            held: Whether the owner's record holds the key now.
            grace_seconds: How old a claim nothing holds must be before it is released.

        Returns:
            True when it was released.
        """
        if held or _age_seconds(claim.claimed_at) < grace_seconds:
            return False
        return self._c.delete(
            claim.key, only_if={"owner": claim.owner, "claimed_at": claim.claimed_at}
        )


def _renewed(owner: str, claim: Claim) -> Claim:
    """*claim* stamped fresh when *owner* holds it, else unchanged."""
    if claim.owner != owner:
        return claim
    return claim.model_copy(update={"claimed_at": _now()})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _age_seconds(stamp: str) -> float:
    """Seconds since *stamp*, an ISO 8601 time. An empty or unreadable one counts as old."""
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return float("inf")
