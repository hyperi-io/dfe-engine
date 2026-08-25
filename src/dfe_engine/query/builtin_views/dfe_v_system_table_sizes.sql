CREATE OR REPLACE VIEW {db}.dfe_v_system_table_sizes AS
SELECT
    database,
    `table`,
    formatReadableSize(sum(bytes_on_disk)) AS size,
    sum(rows) AS total_rows,
    count() AS parts
FROM system.parts
WHERE active
  AND database = {org_id:String}
GROUP BY database, `table`
ORDER BY sum(bytes_on_disk) DESC
LIMIT {limit:UInt32}
