#  Project:      dfe-engine
#  File:         api/e2e/seed/hunts.py
#  Purpose:      Hunt seeder primitives for e2e-server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Hunt seeding. Public methods are the scripts. Privates are reusable.

A seeded hunt is a stored config the list can search. It is not run: the
customer is the org ``seed_organisation`` writes, and each hunt names the
rule of the same index so the two seeds line up when both have been called.
"""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.api.e2e.seed.sources import SEED_ACTOR

_SEEDED_ORG = "organisation"
"""The org ``Organisations.seed_organisation`` writes, named here as the customer."""

_CRON = "*/5 * * * *"


def searchable_hunt_names() -> list[str]:
    """Hunt file stems ``seed_searchable_hunts`` writes."""
    return [f"searchable-hunt-{num}" for num in range(50)]


def _hunt_config(name: str, num: int) -> dict:
    """One stored hunt config, in the shape the hunt list reads."""
    return {
        "display_name": f"Searchable Hunt {num}",
        "cron": _CRON,
        "log_buffer": 60,
        "customers": [_SEEDED_ORG],
        "rules": [{"rule_name": f"searchable-rule-{num}"}],
    }


class Hunts(Seed):
    """Create or reset hunt configs in the hunt registry."""

    def seed_searchable_hunts(self) -> bool:
        """Seed fifty hunts a list search can filter.

        Returns True when any hunt was written, False when they were all already there.
        """
        registry = self._require_hunt_registry()
        pending = {
            name: _hunt_config(name, num)
            for num, name in enumerate(searchable_hunt_names())
            if not registry.name_exists(name)
        }
        if not pending:
            return False
        registry.save_many(
            pending,
            created_by=SEED_ACTOR,
            scope="e2e",
            summary="seed searchable hunts",
        )
        return True

    def delete_all(self) -> None:
        """Clear every hunt config. A no-op when no hunt registry is configured."""
        registry = self._hunt_registry
        if registry is None:
            return
        for row in registry.list_hunts():
            registry.delete(str(row["name"]), created_by=SEED_ACTOR)
