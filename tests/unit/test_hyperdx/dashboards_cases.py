#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/dashboards_cases.py
#  Purpose:      Case tables for the shipped HyperDX dashboards
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the shipped HyperDX dashboards."""

from typing import TypedDict


class EpochGuardCase(TypedDict):
    id: str
    column: str
    tile_id: str


# Throughput tiles whose time maths would read a member's missing column as 1970.
EPOCH_GUARD_CASES: list[EpochGuardCase] = [
    {"id": "lag_p95", "column": "_timestamp_received", "tile_id": "lag-p95"},
    {"id": "lag_max", "column": "_timestamp_received", "tile_id": "lag-max"},
    {"id": "lag_percentiles", "column": "_timestamp_received", "tile_id": "lag-percentiles"},
    {"id": "event_skew_event_time", "column": "_timestamp", "tile_id": "event-skew"},
    {"id": "event_skew_received", "column": "_timestamp_received", "tile_id": "event-skew"},
]
