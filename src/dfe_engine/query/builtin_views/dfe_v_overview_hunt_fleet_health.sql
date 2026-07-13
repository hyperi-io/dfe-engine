-- Hunt-fleet health overview: enabled hunts, too-aggressive hunts, hunts with
-- overruns. The hunt-runner coordination tables (hunt_schedule / hunt_state) live
-- in the DATA database, which defaults to `dfe`; hardcoded here to match the
-- sibling overview views (dfe.default, dfe_hunts.detection). NOTE: a deployment
-- that renames effective_data_database needs these two refs re-templated - the
-- builtin-view loader (query/ddl.py) currently applies the SQL verbatim, so a
-- proper fix templates the data db across all overview views (flagged follow-up).
CREATE OR REPLACE VIEW dfe_v_overview_hunt_fleet_health AS
SELECT
    (SELECT count() FROM (
        SELECT hunt_id, argMax(enabled, updated) AS e FROM dfe.hunt_schedule GROUP BY hunt_id
    ) WHERE e = 1) AS enabled_hunts,
    (SELECT count() FROM (
        SELECT hunt_id, argMax(too_aggressive, updated) AS ta FROM dfe.hunt_state GROUP BY hunt_id
    ) WHERE ta = 1) AS too_aggressive_hunts,
    (SELECT count() FROM (
        SELECT hunt_id, argMax(overrun_count, updated) AS o FROM dfe.hunt_state GROUP BY hunt_id
    ) WHERE o > 0) AS hunts_with_overruns
