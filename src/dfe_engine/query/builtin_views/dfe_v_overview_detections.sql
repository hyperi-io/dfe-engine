CREATE OR REPLACE VIEW dfe_v_overview_detections AS
SELECT
    toStartOfInterval(_timestamp_load, INTERVAL {bucket_minutes:UInt32} MINUTE) AS bucket,
    coalesce(severity, 'unknown') AS severity,
    count() AS detections,
    uniqExact(hunt_name) AS hunts_firing
FROM dfe_hunts.detection
WHERE _timestamp_load >= {time_from:DateTime64(3)}
  AND _timestamp_load < {time_to:DateTime64(3)}
GROUP BY bucket, severity
ORDER BY bucket, detections DESC
