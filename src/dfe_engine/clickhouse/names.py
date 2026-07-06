#  Project:      dfe-engine
#  File:         clickhouse/names.py
#  Purpose:      Canonical ClickHouse database + table name constants (SSoT)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Single source of truth for the engine's FIXED ClickHouse db/table identifiers.

No call site should hardcode an internal db/table magic string - import the
constant here instead. The enforcement guard (see ``guard.py``) fails CI on a
hardcoded internal db/table literal outside this module.

NOT here: the DATA database. It is deployment-dynamic and read via
``settings.clickhouse.effective_data_database`` (the good, already-centralised
pattern). Source/data tables AND the hunt-runner coordination tables
(``hunt_lease``/``hunt_watermark``/``hunt_state``/``hunt_schedule``) live in that
data database, qualified at the call site - only their TABLE names are fixed here.
"""

from __future__ import annotations

from typing import Final

# -- Fixed engine-owned databases (distinct from the tenant DATA db) -----------
DFE: Final = "dfe"  # default data-database NAME (the value effective_data_database defaults to)
DFE_HUNTS: Final = "dfe_hunts"  # detections + hunt outputs (dfe_hunts.detection)
DFE_AUDIT: Final = "dfe_audit"  # audit / alert cooldown state
DFE_META: Final = "dfe_meta"  # governance projections (orgs, ch_tiers)
DFE_INTERNAL: Final = "dfe_internal"  # engine-only small-object store; hidden from HyperDX CH users

# -- Fixed tables, qualified by the database noted -----------------------------
# dfe_audit
ALERT_STATE: Final = "alert_state"  # ReplacingMergeTree cooldown per (hunt,rule,customer,group_key)
QUERY_LOG_ARCHIVE: Final = (
    "query_log_archive"  # system.query_log -> DFE cost/attribution archive (MV target)
)
# dfe_meta
ORGS: Final = "orgs"  # tenant-axis projection (dfe_meta.orgs)
CH_TIERS: Final = "ch_tiers"  # storage-tier projection (dfe_meta.ch_tiers)
# dfe_internal
REPOSITORY: Final = "repository"  # scope-aligned UI-prefs / JSON / small-file store
# dfe_hunts
DETECTION: Final = "detection"  # detection results table

# -- Hunt-runner coordination tables: live in the DATA db (effective_data_database) --
HUNT_LEASE: Final = "hunt_lease"  # who holds a hunt now, until when (mutual exclusion)
HUNT_WATERMARK: Final = "hunt_watermark"  # last committed window end per hunt (incremental resume)
HUNT_STATE: Final = "hunt_state"  # overrun_count / too_aggressive (UI signal)
HUNT_SCHEDULE: Final = "hunt_schedule"  # deterministic schedule KEDA scales on
# data-table with versioned DDL rendered via schema/ (effective_data_database)
DETECTION_CHECKPOINT: Final = "detection_checkpoint"
