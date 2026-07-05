CREATE OR REPLACE VIEW dfe_v_overview_alerts AS
SELECT
    hunt_name,
    rule_name,
    sum(fire_count) AS fires,
    sum(suppressed_count) AS suppressed,
    max(last_fired_at) AS last_fired
FROM dfe_audit.alert_state FINAL
GROUP BY hunt_name, rule_name
ORDER BY fires DESC
LIMIT {limit:UInt32}
