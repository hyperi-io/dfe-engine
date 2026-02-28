CREATE OR REPLACE VIEW dfe_v_system_health AS
SELECT
    hostname() AS host,
    version() AS version,
    uptime() AS uptime_seconds,
    currentDatabase() AS current_database,
    (SELECT count() FROM system.parts WHERE active) AS active_parts,
    (SELECT count() FROM system.tables WHERE database = {org_id:String}) AS table_count,
    (SELECT formatReadableSize(sum(bytes_on_disk)) FROM system.parts WHERE active AND database = {org_id:String}) AS total_size
