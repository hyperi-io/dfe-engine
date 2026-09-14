#  Project:      dfe-engine
#  File:         orgs/seed.py
#  Purpose:      Create the organisations a deployment names in its settings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Organisation seeding from settings.

A seeded org is created only when the registry does not hold it, so an org edited
in the console keeps its edits across restarts, and nothing is ever deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.auth.audit import audit_org_change

if TYPE_CHECKING:
    from dfe_engine.orgs.registry import OrgRegistry
    from dfe_engine.settings import SeedOrg

SEED_ACTOR = "engine"


def seed_orgs(*, registry: OrgRegistry, seeds: list[SeedOrg]) -> list[str]:
    created = []
    for seed in seeds:
        if registry.get(seed.name) is not None:
            continue
        registry.create(display_name=seed.display_name, name=seed.name, org_ids=seed.org_ids)
        audit_org_change(admin_id=SEED_ACTOR, change="created", org_name=seed.name)
        logger.info(f"Seeded organisation {seed.name!r}")
        created.append(seed.name)
    return created
