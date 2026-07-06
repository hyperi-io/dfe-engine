#  Project:      dfe-engine
#  File:         clickhouse/profiles.py
#  Purpose:      Per-query-class ClickHouse settings profiles (timeout + settings)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Per-query-class ClickHouse settings profiles.

Each query CLASS gets its own server-settings baseline + transport timeout, so a
bounded user READ cannot inherit an admin path's unbounded budget, and a
migration gets the long ON CLUSTER DDL window. Pattern reimplemented from Snuba's
``ClickhouseClientSettings`` (FSL - idea, not source).

The profile is part of the connection-cache key (``connection.py``): each profile
maps to its OWN pooled client, because the transport ``timeout_s`` is a
per-client property in clickhouse-connect. The ``settings`` dict is the profile's
BASELINE server settings, merged under the per-tenant / caller / governance layers
at call time (see the execution facade).

Timeout unit gotcha (from the reference audit): clickhouse-connect transport
timeouts are in SECONDS (unlike clickhouse-driver's milliseconds) - the numbers
here are seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    """A query class's baseline server settings + transport timeout (seconds).

    settings:   server settings applied to every call on this profile's client
                (baseline; caller/tenant/governance layers merge ON TOP).
    timeout_s:  clickhouse-connect ``send_receive_timeout`` for the pooled client,
                in SECONDS. ``None`` = the driver default (no explicit cap).
    """

    settings: dict[str, Any]
    timeout_s: float | None


class Profile(StrEnum):
    """The query CLASS a call declares - selects the settings profile + pool.

    QUERY     - user-facing bounded reads (25s exec cap, short transport window).
    INTERNAL  - admin / topology introspection; deliberately UNBOUNDED (it must
                NOT inherit QUERY's cap, or sensing/reconcile can time out).
    MIGRATE   - DDL incl ON CLUSTER: server-side sync (alter/mutations) + a long
                distributed-DDL window so every replica acks before we return.
    INSERT    - bulk writes (no exec cap; the writer owns its own batching).
    DELETE    - lightweight mutations, synchronous.
    OPTIMIZE  - OPTIMIZE TABLE (long, single).
    TRACING   - read-with-settings-changeable (readonly=2) diagnostic path.
    """

    QUERY = "query"
    INTERNAL = "internal"
    MIGRATE = "migrate"
    INSERT = "insert"
    DELETE = "delete"
    OPTIMIZE = "optimize"
    TRACING = "tracing"

    @property
    def spec(self) -> ProfileSpec:
        return _SPECS[self]


# Kept as a member->spec map (not the enum value) so two profiles with an
# identical spec do not collapse into an Enum ALIAS.
_SPECS: Final[dict[Profile, ProfileSpec]] = {
    Profile.QUERY: ProfileSpec(settings={"max_execution_time": 25}, timeout_s=30.0),
    Profile.INTERNAL: ProfileSpec(settings={}, timeout_s=None),
    Profile.MIGRATE: ProfileSpec(
        settings={
            # Server-side synchronisation IS the wait - do not add a client poll.
            "alter_sync": 2,
            "mutations_sync": 2,
            "distributed_ddl_task_timeout": 300,
            "database_atomic_wait_for_drop_and_detach_synchronously": 1,
        },
        timeout_s=300.0,
    ),
    Profile.INSERT: ProfileSpec(settings={}, timeout_s=None),
    Profile.DELETE: ProfileSpec(settings={"mutations_sync": 1}, timeout_s=None),
    Profile.OPTIMIZE: ProfileSpec(settings={}, timeout_s=300.0),
    # readonly=2 = read-only BUT per-query settings still apply (not readonly=1).
    Profile.TRACING: ProfileSpec(settings={"readonly": 2}, timeout_s=None),
}
