#  Project:      dfe-engine
#  File:         orgs/seed.py
#  Purpose:      Create the organisations a deployment names in its settings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Organisation seeding from settings.

A seeded org is created only when the registry does not hold it, so an org edited
in the console keeps its edits across restarts, and nothing is ever deleted. The seed
rewrites no hunt, so a hunt naming a seeded org keeps what it stored: one whose org
was deleted holds no ids for it and stays refused until it is edited.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scalo.logger import logger

from dfe_engine.auth.audit import audit_org_change
from dfe_engine.gitops.repo import GitopsRemoteError
from dfe_engine.orgs.tenant_scope import org_tenant_ids

if TYPE_CHECKING:
    from dfe_engine.orgs.registry import OrgRegistry
    from dfe_engine.settings import SeedOrg

SEED_ACTOR = "engine"


def seed_orgs(
    *, registry: OrgRegistry, seeds: list[SeedOrg], hunts: Any | None = None
) -> list[str]:
    """Create each seed the registry lacks, unless a hunt reads one of its ids for another org.

    A hunt counts on main or on a review branch not yet merged, as on the org routes. A
    seed the forge cannot check is skipped with a warning, so a down forge never holds
    up startup, and the next boot seeds it.

    Args:
        registry: The org registry.
        seeds: ``orgs.seed_orgs``.
        hunts: The hunt registry, or None where the deployment carries none.

    Returns:
        The orgs created, in seed order.
    """
    created = []
    for seed in seeds:
        if registry.get(seed.name) is not None:
            continue
        try:
            held = (
                hunts.tenant_id_holders(seed.name, org_tenant_ids(seed))
                if hunts is not None
                else []
            )
        except GitopsRemoteError as exc:
            logger.warning(
                "organisation not seeded: the deploy repo could not be read to check its "
                "tenant ids, so the next boot seeds it",
                org=seed.name,
                error=str(exc),
            )
            continue
        if held:
            logger.warning(
                "organisation not seeded: a hunt still reads one of its tenant ids for another org",
                org=seed.name,
                hunts=sorted({h.hunt for h in held}),
            )
            continue
        registry.create(display_name=seed.display_name, name=seed.name, org_ids=seed.org_ids)
        audit_org_change(admin_id=SEED_ACTOR, change="created", org_name=seed.name)
        logger.info(f"Seeded organisation {seed.name!r}")
        created.append(seed.name)
    return created
