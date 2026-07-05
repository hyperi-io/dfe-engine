CREATE OR REPLACE VIEW dfe_v_overview_ingest_rows_bytes AS
SELECT
    toStartOfInterval(_timestamp_load, INTERVAL {bucket_minutes:UInt32} MINUTE) AS bucket,
    count() AS rows,
    sum(length(coalesce(_raw, ''))) AS approx_bytes
FROM dfe.default
WHERE _timestamp_load >= {time_from:DateTime64(3)}
  AND _timestamp_load < {time_to:DateTime64(3)}
GROUP BY bucket
ORDER BY bucket
