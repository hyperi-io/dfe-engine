#  Project:      dfe-engine
#  File:         hyperdx/__init__.py
#  Purpose:      HyperDX integration package init
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""HyperDX integration — team and connection management via internal API."""

from dfe_engine.hyperdx.client import HyperDXClient

__all__ = ["HyperDXClient"]
