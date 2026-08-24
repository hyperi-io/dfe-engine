-- Hunt-fleet health overview: enabled hunts, too-aggressive hunts, hunts with
-- overruns. `{db}` is substituted with the deployment's data database by the
-- builtin-view loader (query/ddl.py) - never write the name here.
CREATE OR REPLACE VIEW {db}.dfe_v_overview_hunt_fleet_health AS
SELECT
    (SELECT count() FROM (
        SELECT hunt_id, argMax(enabled, updated) AS e FROM {db}.hunt_schedule GROUP BY hunt_id
    ) WHERE e = 1) AS enabled_hunts,
    (SELECT count() FROM (
        SELECT hunt_id, argMax(too_aggressive, updated) AS ta FROM {db}.hunt_state GROUP BY hunt_id
    ) WHERE ta = 1) AS too_aggressive_hunts,
    (SELECT count() FROM (
        SELECT hunt_id, argMax(overrun_count, updated) AS o FROM {db}.hunt_state GROUP BY hunt_id
    ) WHERE o > 0) AS hunts_with_overruns
