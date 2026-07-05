CREATE OR REPLACE VIEW dfe_v_overview_hunt_cost_leaderboard AS
SELECT
    hunt_name,
    rule_name,
    count() AS runs,
    sum(read_bytes) AS read_bytes,
    sum(read_rows) AS read_rows,
    avg(execution_time_ms) AS avg_ms,
    max(memory_usage) AS peak_mem_bytes
FROM dfe_audit.detection_checkpoint
WHERE query_checkpoint_time >= {time_from:DateTime}
GROUP BY hunt_name, rule_name
ORDER BY read_bytes DESC
LIMIT {limit:UInt32}
