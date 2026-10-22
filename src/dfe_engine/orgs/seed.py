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
from dfe_engine.orgs.models import DEFAULT_ORG_ID, DEFAULT_ORG_NAME
from dfe_engine.orgs.registry import OrgExistsError
from dfe_engine.orgs.tenant_scope import org_tenant_ids

if TYPE_CHECKING:
    from dfe_engine.orgs.registry import AnyOrgRegistry
    from dfe_engine.settings import SeedOrg

SEED_ACTOR = "engine"


def ensure_default_org(registry: AnyOrgRegistry) -> bool:
    """Create the reserved default org, or restore its tenant id and enable it.

    Every app stamps ``DEFAULT_ORG_ID`` on a record that names no org, so the org
    holding that tenant id has to exist for those rows to belong to anyone.

    Args:
        registry: The org registry.

    Returns:
        Whether anything was written.

    Raises:
        ValueError: Another org already holds the reserved name's tenant id, so the
            default org's rows would be that org's to read.
    """
    held = registry.get(DEFAULT_ORG_NAME)
    if held is None:
        try:
            registry.create(DEFAULT_ORG_NAME, org_ids=[DEFAULT_ORG_ID], display_name="Default")
        except OrgExistsError:
            # A replica booting beside this one wrote it between the read and the create.
            return ensure_default_org(registry)
        audit_org_change(admin_id=SEED_ACTOR, change="created", org_name=DEFAULT_ORG_NAME)
        logger.info("Created the reserved default organisation", org=DEFAULT_ORG_NAME)
        return True
    if held.enabled and DEFAULT_ORG_ID in org_tenant_ids(held):
        return False
    org_ids = [*held.org_ids, DEFAULT_ORG_ID] if held.org_ids else [DEFAULT_ORG_ID]
    registry.update(DEFAULT_ORG_NAME, org_ids=list(dict.fromkeys(org_ids)), enabled=True)
    audit_org_change(admin_id=SEED_ACTOR, change="updated", org_name=DEFAULT_ORG_NAME)
    logger.warning(
        "Restored the reserved default organisation's tenant id and enabled it",
        org=DEFAULT_ORG_NAME,
    )
    return True


def seed_orgs(
    *,
    registry: AnyOrgRegistry,
    seeds: list[SeedOrg],
    hunts: Any | None = None,
) -> list[str]:
    """Create each seed the registry lacks, unless a hunt reads one of its ids for another org.

    A hunt counts on main or on a review branch not yet merged, as on the org routes. A
    seed the forge cannot check within ``gitops.read`` is skipped with a warning, so a
    down forge never holds up startup, and the next boot seeds it. A seed another
    replica created first is left as that replica wrote it.

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
                hunts.tenant_id_holders(seed.name, org_tenant_ids(seed), bound="read")
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
        try:
            registry.create(display_name=seed.display_name, name=seed.name, org_ids=seed.org_ids)
        except OrgExistsError:
            continue
        audit_org_change(admin_id=SEED_ACTOR, change="created", org_name=seed.name)
        logger.info(f"Seeded organisation {seed.name!r}")
        created.append(seed.name)
    return created
