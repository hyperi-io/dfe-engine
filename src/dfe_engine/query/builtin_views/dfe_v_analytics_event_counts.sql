CREATE OR REPLACE VIEW dfe_v_analytics_event_counts AS
SELECT
    toDate(timestamp) AS event_date,
    event_type,
    count() AS event_count
FROM events
WHERE org_id = {org_id:String}
  AND timestamp >= {time_from:DateTime64(3)}
  AND timestamp < {time_to:DateTime64(3)}
GROUP BY event_date, event_type
ORDER BY event_date DESC, event_count DESC
LIMIT {limit:UInt32}
