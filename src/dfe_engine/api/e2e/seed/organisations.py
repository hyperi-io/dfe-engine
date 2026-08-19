#  Project:      dfe-engine
#  File:         api/e2e/seed/accounts.py
#  Purpose:      Account seeder primitives for e2e-server and sibling seeders
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local-account seeding. Public methods are the scripts; privates are reusable."""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed

_DEFAULT_ORGANISATION_NAME = "organisation"


class Organisations(Seed):
    """Create or reset local organisations."""

    def seed_organisation(
        self,
        name: str = _DEFAULT_ORGANISATION_NAME,
        display_name: str = _DEFAULT_ORGANISATION_NAME,
    ) -> bool:
        """Seed a local organisation in ``dfe-organisations``."""
        return True
