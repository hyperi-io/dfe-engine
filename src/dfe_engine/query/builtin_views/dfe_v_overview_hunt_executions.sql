CREATE OR REPLACE VIEW dfe_v_overview_hunt_executions AS
SELECT
    toStartOfInterval(query_checkpoint_time, INTERVAL {bucket_minutes:UInt32} MINUTE) AS bucket,
    count() AS executions,
    uniqExact(hunt_name) AS hunts,
    avg(execution_time_ms) AS avg_ms,
    quantile(0.95)(execution_time_ms) AS p95_ms,
    sum(read_rows) AS read_rows,
    sum(read_bytes) AS read_bytes,
    sum(result_rows) AS result_rows
FROM dfe_audit.detection_checkpoint
WHERE query_checkpoint_time >= {time_from:DateTime}
  AND query_checkpoint_time < {time_to:DateTime}
GROUP BY bucket
ORDER BY bucket
