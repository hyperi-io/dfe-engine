CREATE OR REPLACE VIEW {db}.dfe_v_overview_pipeline_lag AS
SELECT
    toStartOfInterval(_timestamp_load, INTERVAL {bucket_minutes:UInt32} MINUTE) AS bucket,
    count() AS rows_landed,
    avg(dateDiff('millisecond', _timestamp_received, _timestamp_load)) AS avg_lag_ms,
    quantile(0.95)(dateDiff('millisecond', _timestamp_received, _timestamp_load)) AS p95_lag_ms
FROM {db}.default
WHERE _timestamp_load >= {time_from:DateTime64(3)}
  AND _timestamp_load < {time_to:DateTime64(3)}
GROUP BY bucket
ORDER BY bucket
