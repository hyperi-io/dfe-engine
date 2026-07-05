CREATE OR REPLACE VIEW dfe_v_overview_ingest_by_source AS
SELECT
    toStartOfInterval(_timestamp_load, INTERVAL {bucket_minutes:UInt32} MINUTE) AS bucket,
    coalesce(_source, 'unknown') AS source,
    count() AS rows,
    sum(length(coalesce(_raw, ''))) AS approx_bytes
FROM dfe.default
WHERE _timestamp_load >= {time_from:DateTime64(3)}
  AND _timestamp_load < {time_to:DateTime64(3)}
GROUP BY bucket, source
ORDER BY bucket, rows DESC
