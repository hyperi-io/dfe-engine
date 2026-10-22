#  Project:      dfe-engine
#  File:         api/e2e/seed/organisations.py
#  Purpose:      Organisation seeder primitives for e2e-server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Organisation seeding. Public methods are the scripts, and privates are reusable.

Each org write moves the hunts naming the org first, through the same calls the org
routes make, so a seeded org is never one its hunts read under stale tenant ids.
"""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.api.e2e.seed.sources import SEED_ACTOR
from dfe_engine.api.v1.orgs import refuse_held_tenant_ids, store_hunt_tenant_ids
from dfe_engine.orgs.models import DEFAULT_ORG_NAME

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

    def delete_all(self) -> None:
        """Clear every org but the reserved default one, and the hunts' ids for each.

        The ingest credentials go first, so no org is left bound by another's.
        """
        for credential in self._org_registry.ingest_credentials.list():
            self._org_registry.ingest_credentials.delete(credential.name)
        for org in self._org_registry.list():
            if org.name == DEFAULT_ORG_NAME:
                continue
            store_hunt_tenant_ids(self._hunt_registry, org.name, None, SEED_ACTOR)
            self._org_registry.delete(org.name)

    def _ensure_organisation(self, name: str, *, display_name: str) -> bool:
        existing = self._org_registry.get(name)
        org_ids = list(existing.org_ids) if existing is not None else []
        if name not in org_ids:
            org_ids.append(name)
        refuse_held_tenant_ids(self._hunt_registry, name, org_ids)
        store_hunt_tenant_ids(self._hunt_registry, name, org_ids, SEED_ACTOR)
        if existing is None:
            self._org_registry.create(name, org_ids=org_ids, display_name=display_name)
            return True
        self._org_registry.update(
            name,
            display_name=display_name,
            enabled=True,
            org_ids=org_ids,
        )
        return False
