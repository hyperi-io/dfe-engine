#  Project:      dfe-engine
#  File:         api/e2e/seed/organisations.py
#  Purpose:      Organisation seeder primitives for e2e-server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Organisation seeding. Public methods are the scripts; privates are reusable."""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed

_DEFAULT_ORGANISATION_NAME = "organisation"


class Organisations(Seed):
    """Create or reset organisations in the YAML org registry."""

    def seed_organisation(
        self,
        name: str = _DEFAULT_ORGANISATION_NAME,
        display_name: str = _DEFAULT_ORGANISATION_NAME,
    ) -> bool:
        """Seed a local organisation. Returns True when created, False when reset."""
        return self._ensure_organisation(name, display_name=display_name)

    def _ensure_organisation(self, name: str, *, display_name: str) -> bool:
        existing = self._org_registry.get(name)
        org_ids = [name]
        if existing is None:
            self._org_registry.create(name, org_ids=org_ids, display_name=display_name)
            return True
        merged_ids = list(existing.org_ids)
        if name not in merged_ids:
            merged_ids.append(name)
        self._org_registry.update(
            name,
            display_name=display_name,
            enabled=True,
            org_ids=merged_ids,
        )
        return False
