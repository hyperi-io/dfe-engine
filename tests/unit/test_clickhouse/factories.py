#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/factories.py
#  Purpose:      Factories for clickhouse unit test inputs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Factories for clickhouse unit test inputs."""

from dfe_engine.clickhouse.all_sources import Coalescer
from dfe_engine.schema.applier import LiveTable
from dfe_engine.source.models import Source


def make_coalescer() -> Coalescer[str]:
    """Build an idle clickhouse.all_sources.Coalescer."""
    return Coalescer[str]()


def make_live_table(*, engine: str) -> LiveTable:
    """Build a schema.applier.LiveTable with no TTL."""
    return LiveTable(engine=engine, ttl=None)


def make_source(*, name: str) -> Source:
    """Build a source.models.Source that matches records by collector type."""
    return Source.model_validate(
        {"match": {"field": "tags.collector.type", "value": name}, "source": name}
    )
