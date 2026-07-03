#  Project:      dfe-engine
#  File:         store.py
#  Purpose:      RepositoryStore - ClickHouse-backed scope-aligned small-object store
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""ClickHouse-backed store for scope-aligned small objects.

Storage model (see ``resources/repository.sql``, canonical copy in the
dfe-schemas submodule at ``ddl/repository.sql``):

- One ReplacingMergeTree(updated_at, is_deleted) table keyed by
  ``(scope, scope_id, namespace, key)``.
- Every write is an INSERT of a full new row - latest ``updated_at`` wins.
- Deletes are tombstone INSERTs (``is_deleted=1``); reads filter them out
  with ``FINAL ... AND is_deleted = 0``. Never ALTER UPDATE/DELETE.
- The etag is the row's ``updated_at`` rendered as an opaque string.
  Timestamps are truncated to millisecond precision client-side so the
  etag survives the DateTime64(3) round-trip.
"""

from __future__ import annotations

import importlib.resources
from datetime import UTC, datetime
from typing import Any

from hyperi_pylib.logger import logger

DEFAULT_DATABASE = "dfe_internal"

# Record/metadata shapes returned by the store. Named because the
# ``list`` method shadows the builtin inside the class body.
Record = dict[str, Any]
RecordList = list[Record]

_GET_SQL = """\
SELECT content_type, value, size, updated_by, updated_at
FROM {db}.repository FINAL
WHERE scope = %(scope)s AND scope_id = %(scope_id)s
  AND namespace = %(namespace)s AND key = %(key)s
  AND is_deleted = 0
LIMIT 1\
"""

_LIST_SQL = """\
SELECT key, content_type, size, updated_by, updated_at
FROM {db}.repository FINAL
WHERE scope = %(scope)s AND scope_id = %(scope_id)s
  AND namespace = %(namespace)s
  AND is_deleted = 0
ORDER BY key\
"""

_INSERT_SQL = """\
INSERT INTO {db}.repository
    (scope, scope_id, namespace, key, content_type, value, size, \
updated_by, updated_at, is_deleted)
VALUES\
"""


class ConflictError(Exception):
    """If-Match etag did not match the currently stored row (HTTP 412)."""

    def __init__(self, message: str, current_etag: str | None = None) -> None:
        super().__init__(message)
        self.current_etag = current_etag


def json_merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7396 JSON Merge Patch.

    Recursive object merge; ``null`` deletes a key; arrays and scalars
    replace wholesale. Implemented locally - no extra dependency.
    """
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = json_merge_patch(result.get(key), value)
    return result


def _load_ddl(database: str) -> str:
    """Load the bundled DDL, substituting an overridden database name."""
    pkg = importlib.resources.files("dfe_engine.repository.resources")
    ddl = pkg.joinpath("repository.sql").read_text(encoding="utf-8")
    if database != DEFAULT_DATABASE:
        ddl = ddl.replace(DEFAULT_DATABASE, database)
    return ddl


def _now_ms() -> datetime:
    """UTC now truncated to millisecond precision (matches DateTime64(3))."""
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _etag(updated_at: datetime) -> str:
    """Render updated_at as the opaque etag string (stable across round-trips)."""
    if updated_at.tzinfo is None:
        # clickhouse-connect returns naive UTC datetimes
        updated_at = updated_at.replace(tzinfo=UTC)
    return updated_at.isoformat(timespec="milliseconds")


def _as_bytes(value: Any) -> bytes:
    """Normalise a stored value to bytes (drivers may hand back str)."""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


class RepositoryStore:
    """Scope-aligned small-object store over ClickHouse.

    Constructed per-request around the pooled client wrapper. Schema is
    ensured lazily once per process per database (class-level flag), so
    unit tests and CH-less startups never touch ClickHouse eagerly.
    """

    _ensured_databases: set[str] = set()

    def __init__(self, client: Any, database: str = DEFAULT_DATABASE) -> None:
        self._client = client
        self._db = database

    # ── Schema ────────────────────────────────────────────────

    def ensure_schema(self) -> None:
        """Create database + table from the bundled DDL (idempotent)."""
        if self._db in RepositoryStore._ensured_databases:
            return
        # Strip SQL comment lines BEFORE splitting on ';' - the header
        # comment itself contains a semicolon, and the client wrapper
        # routes on the first keyword, which must be the CREATE itself.
        lines = [ln for ln in _load_ddl(self._db).splitlines() if not ln.strip().startswith("--")]
        for statement in "\n".join(lines).split(";"):
            stmt = statement.strip()
            if stmt:
                self._client.execute(stmt)
        RepositoryStore._ensured_databases.add(self._db)
        logger.debug(f"RepositoryStore: ensured {self._db}.repository exists")

    # ── CRUD ──────────────────────────────────────────────────

    def get(self, scope: str, scope_id: str, namespace: str, key: str) -> dict[str, Any] | None:
        """Return the current record for a key, or None (tombstones hidden)."""
        self.ensure_schema()
        rows = self._client.execute(
            _GET_SQL.format(db=self._db),
            parameters={
                "scope": scope,
                "scope_id": scope_id,
                "namespace": namespace,
                "key": key,
            },
        )
        if not rows:
            return None
        content_type, value, size, updated_by, updated_at = rows[0]
        return {
            "scope": scope,
            "scope_id": scope_id,
            "namespace": namespace,
            "key": key,
            "content_type": content_type,
            "value": _as_bytes(value),
            "size": size,
            "updated_by": updated_by,
            "updated_at": updated_at,
            "etag": _etag(updated_at),
        }

    def put(
        self,
        scope: str,
        scope_id: str,
        namespace: str,
        key: str,
        value: bytes,
        content_type: str,
        updated_by: str,
        if_match: str | None = None,
    ) -> dict[str, Any]:
        """INSERT a new row (latest updated_at wins). Returns the row metadata.

        Raises ConflictError when ``if_match`` does not equal the stored etag.
        """
        self.ensure_schema()
        if if_match is not None:
            current = self.get(scope, scope_id, namespace, key)
            current_etag = current["etag"] if current else None
            if current_etag != if_match:
                raise ConflictError(
                    f"If-Match '{if_match}' does not match stored etag",
                    current_etag=current_etag,
                )
        updated_at = _now_ms()
        self._client.execute(
            _INSERT_SQL.format(db=self._db),
            [
                (
                    scope,
                    scope_id,
                    namespace,
                    key,
                    content_type,
                    value,
                    len(value),
                    updated_by,
                    updated_at,
                    0,
                )
            ],
        )
        return {
            "scope": scope,
            "scope_id": scope_id,
            "namespace": namespace,
            "key": key,
            "content_type": content_type,
            "size": len(value),
            "updated_by": updated_by,
            "updated_at": updated_at,
            "etag": _etag(updated_at),
        }

    def delete(self, scope: str, scope_id: str, namespace: str, key: str) -> bool:
        """Tombstone a key (is_deleted=1 INSERT). Returns False if absent."""
        self.ensure_schema()
        current = self.get(scope, scope_id, namespace, key)
        if current is None:
            return False
        self._client.execute(
            _INSERT_SQL.format(db=self._db),
            [(scope, scope_id, namespace, key, "", b"", 0, "", _now_ms(), 1)],
        )
        return True

    def list(self, scope: str, scope_id: str, namespace: str) -> RecordList:
        """Metadata for all live keys in a (scope, scope_id, namespace)."""
        self.ensure_schema()
        rows = self._client.execute(
            _LIST_SQL.format(db=self._db),
            parameters={"scope": scope, "scope_id": scope_id, "namespace": namespace},
        )
        return [
            {
                "key": key,
                "content_type": content_type,
                "size": size,
                "updated_by": updated_by,
                "updated_at": updated_at,
                "etag": _etag(updated_at),
            }
            for key, content_type, size, updated_by, updated_at in rows
        ]
