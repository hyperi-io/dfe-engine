#  Project:      dfe-engine
#  File:         api/e2e/seed/__init__.py
#  Purpose:      e2e-server seeders (test-env gated)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Seed helpers for ``make e2e-server``. Construction requires ``DFE_ENV=test``."""

from dfe_engine.api.e2e.seed.base import Seed

__all__ = ["Seed"]
