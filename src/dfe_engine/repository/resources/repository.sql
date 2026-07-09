-- dfe_internal.repository - scope-aligned small-object store (UI prefs, JSON, small files).
-- Engine-only: the dfe_internal database is granted solely to the engine's own
-- ClickHouse user - never to HyperDX per-group users. No _org_id column by design
-- (keeps the ChRbacReconciler's _org_id discovery away from this table).
-- Writes are always INSERTs (latest updated_at wins); deletes are tombstone rows.
CREATE DATABASE IF NOT EXISTS dfe_internal;

CREATE TABLE IF NOT EXISTS dfe_internal.repository
(
    scope        LowCardinality(String),
    scope_id     String,
    namespace    LowCardinality(String),
    key          String,
    content_type LowCardinality(String),
    value        String CODEC(ZSTD(3)),
    size         UInt32,
    updated_by   String,
    updated_at   DateTime64(3),
    is_deleted   UInt8 DEFAULT 0
)
ENGINE = ReplacingMergeTree(updated_at, is_deleted)
ORDER BY (scope, scope_id, namespace, key);
