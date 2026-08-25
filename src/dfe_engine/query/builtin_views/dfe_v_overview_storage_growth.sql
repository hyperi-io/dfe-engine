CREATE OR REPLACE VIEW {db}.dfe_v_overview_storage_growth AS
SELECT
    database,
    `table`,
    partition AS day,
    sum(rows) AS rows,
    sum(bytes_on_disk) AS bytes_on_disk,
    sum(data_uncompressed_bytes) AS uncompressed_bytes
FROM system.parts
WHERE active
  AND database = '{db}'
GROUP BY database, `table`, partition
ORDER BY database, `table`, day
