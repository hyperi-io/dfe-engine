#  Project:      dfe-engine
#  File:         ingest/__init__.py
#  Purpose:      The ingest rule set's inputs beyond the sources: compatibility profiles and order
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What dfe-receiver's rule set compiles from, besides each source's own rows.

A compatibility profile is a versioned block of rule rows that reads another
platform's header into the DFE header. dfe-schemas ships the core ones, the
engine seeds them into the deploy repo, and each source binds them by id. The
rule order says where each source's rows and each profile's block run, per target.
"""

from dfe_engine.ingest.models import (
    ALIASES_TABLE,
    CompatProfile,
    CompatProfileVersion,
    IngestRules,
    RuleOrder,
)

__all__ = [
    "ALIASES_TABLE",
    "CompatProfile",
    "CompatProfileVersion",
    "IngestRules",
    "RuleOrder",
]
