#  Project:      dfe-engine
#  File:         synthetic_data/__init__.py
#  Purpose:      Realistic test/demo data generation (reference packs + streams)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Realistic test/demo data generation.

Generates lookalike source events for testing, demos and sales - a live-feeling
stream at modest rates, never a load generator. Two modes:

- **schema** (default): drive generation from a dfe-schemas meta schema. The
  ``@source:`` exprs give the source JSON shape; column name/type/use_case
  heuristics pick semantic generators (IPs, FQDNs, users, ...); optional
  per-column ``synthetic:`` hints in the schema YAML supply curated vocabularies
  and message templates where heuristics cannot reach.
- **sample**: lookalike generation from a logreducer-reduced sample (later
  stage - see the synthetic data plan).

A seeded :class:`~dfe_engine.synthetic_data.entities.EntityPool` keeps events
coherent: the same host always carries the same FQDN/IP/MAC and its users,
and an identical seed reproduces the identical stream.
"""

from dfe_engine.synthetic_data.entities import EntityPool
from dfe_engine.synthetic_data.models import ColumnHints, SyntheticDataError
from dfe_engine.synthetic_data.schema_source import SchemaEventFactory
from dfe_engine.synthetic_data.stream import CollectSink, HttpPostSink, stream_events

__all__ = [
    "CollectSink",
    "ColumnHints",
    "SyntheticDataError",
    "EntityPool",
    "HttpPostSink",
    "SchemaEventFactory",
    "stream_events",
]
