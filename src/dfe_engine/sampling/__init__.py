#  Project:      dfe-engine
#  File:         sampling/__init__.py
#  Purpose:      Source sampler - pull sample data from ClickHouse or Kafka
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Source sampler.

Pull a small, inspectable slice of a source's events from ClickHouse (landed
``_json``) or Kafka (a topic), in one of four modes: ``recent`` (fast tail),
``random`` (uniform), ``smart`` (logreducer representative sample - the default)
and ``anomaly`` (logreducer outliers). One service (``Sampler``) backs both the
API router and the ``dfe sample`` CLI. Consumers: the CLI, the UI (charts),
downstream processing APIs, and the AI plug-in.
"""

from .models import (
    GATED_MODES,
    SampleBackend,
    SampleMode,
    SampleRequest,
    SamplerError,
    SampleResult,
    SampleScopeError,
)
from .service import Sampler

__all__ = [
    "GATED_MODES",
    "Sampler",
    "SampleBackend",
    "SampleMode",
    "SampleRequest",
    "SampleResult",
    "SampleScopeError",
    "SamplerError",
]
