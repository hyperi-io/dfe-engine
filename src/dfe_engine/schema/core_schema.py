#  Project:      dfe-engine
#  File:         schema/core_schema.py
#  Purpose:      Where the core schema lands, and the landing table's applied DDL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What the deployment tells the schema phase, and what the phase applied.

The table LIST used to live here as three Python tuples. It is now
``manifest.yaml`` in dfe-schemas, read by :mod:`dfe_engine.schema.plan` and
applied by :mod:`dfe_engine.schema.phase`, so adding a table is a manifest edit
and never an engine release.

What is left is the deployment's side of that: which database the objects land
in, the landing table's profile, and the retention a time-series table gets when
it declares none. All of them land in ONE database,
``clickhouse.data_database`` (default ``dfe``), telemetry included -- org
isolation is by row policy, not by splitting databases (see
:mod:`dfe_engine.governance.ch.models`).

The landing table's name is not among them: :func:`landing_table_name` reads it
from the manifest and no setting moves it.

The SOC2 audit trail is not among them: it is structured log events through the
OTel pipeline, with no table behind it (see :mod:`dfe_engine.auth.audit`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The landing table's manifest id. The NAME is whatever the definition declares,
# read off the rendered object -- a literal here is how the engine and
# dfe-schemas came to disagree about it in the first place.
LANDING_TABLE_ID = "data.main"


@dataclass(frozen=True)
class CoreSchemaTargets:
    """Where the core schema lands: the DFE database and the landing table's profile."""

    database: str
    profile: str = "timeseries"
    # Retention for a time-series table that declares none; 0 removes its TTL, None leaves it alone.
    default_ttl_days: int | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> CoreSchemaTargets:
        """Read the targets off ``settings.clickhouse``."""
        return cls.from_clickhouse(settings.clickhouse)

    @classmethod
    def from_clickhouse(cls, ch: Any) -> CoreSchemaTargets:
        """Read the targets off a ``ClickHouseSettings`` directly.

        The schema tooling loads only that section, because the full settings
        model validates an API posture it does not have.
        """
        return cls(
            database=ch.effective_data_database,
            profile=ch.default_table_profile,
            default_ttl_days=ch.default_ttl_days,
        )


def landing_table_name() -> str:
    """The landing table's name, as the dfe-schemas manifest declares it.

    This is the name the bootstrap creates, so every reader and writer takes it from
    here and never from a setting or a literal.
    """
    from dfe_engine.schema.plan import object_names

    return object_names(LANDING_TABLE_ID)[LANDING_TABLE_ID]


def landing_table_ddl(settings: Any) -> str:
    """The landing table's CREATE TABLE, exactly as the phase applies it.

    Rendered from the manifest rather than rebuilt beside it: the source build
    path renders its own DDL from the stored schema, which is not the same
    statement, and anything recording what the bootstrap deployed has to say what
    actually ran.
    """
    from dfe_engine.schema.plan import render_one

    targets = CoreSchemaTargets.from_settings(settings)
    return render_one(
        LANDING_TABLE_ID,
        data_database=targets.database,
        default_ttl_days=targets.default_ttl_days or None,
    ).statements[0]
