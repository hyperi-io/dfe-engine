CREATE OR REPLACE VIEW dfe_v_overview_active_sources AS
SELECT
    uniqExact(_source) AS active_sources,
    uniqExact(_org_id) AS active_orgs,
    count() AS rows_last_hour
FROM dfe.default
WHERE _timestamp_load >= now() - INTERVAL 1 HOUR
