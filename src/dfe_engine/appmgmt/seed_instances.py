#  Project:      dfe-engine
#  File:         appmgmt/seed_instances.py
#  Purpose:      Seed a deployment's default app instances into its deploy repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The default app set a deployment starts with, written into the deploy repo.

An app instance IS its values overlay, so a deploy repo holding none deploys no
apps and the API lists no instances. On Kubernetes the forgejo setup job writes
that first set as it creates the repo; a Compose deployment has no such job, so
the engine writes it at startup - from the same manifest, with the same marker,
so the two seeds cannot disagree about what a profile runs.

Only single-deployment apps are seeded. A per-config app's instance IS a source's
processing step, so one appears when the source is written and ``derived`` is
what writes it.

The marker records what this deployment has already been OFFERED, which is a
different question from what ``values/`` currently holds: deleting an overlay is
how an operator turns an app off, and the seed must never bring it back.
"""

from __future__ import annotations

from typing import Any

from scalo.logger import logger

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.yaml_utils import yaml_dump_string

from . import instances
from .catalogue import APP_CATALOGUE, Multiplicity
from .derived import ENGINE_ACTOR

SEED_INSTANCE = "default"
"""Instance name every seeded overlay carries; the forgejo job's ``seedInstance``."""

MARKER_FILE = ".seeded-apps"
"""Repo-root file listing the offered apps, one per line; the job's ``seedMarker``."""


def default_services(profile: str) -> list[str]:
    """The single-deployment apps this profile runs without being asked.

    The same rule dfe-infra's composition applies to build the k8s seed list, read
    from the same manifest: an app may be deployed where it is offered, and is
    deployed unasked where its ``default_in`` says so or says nothing at all.
    """
    return sorted(
        service
        for service, app in APP_CATALOGUE.items()
        if app.multiplicity is Multiplicity.SINGLE
        and app.offered_in(profile)
        and (app.default_in is None or profile in app.default_in)
    )


def _already_offered(gc: GitCrud) -> set[str]:
    """The apps this deployment has already been offered.

    An overlay present now counts as offered whether or not the marker names it,
    which covers both a repo predating the marker and an overlay somebody wrote by
    hand - neither is overwritten by the seed.
    """
    offered = {i.service for i in instances.list_instances(gc) if i.instance == SEED_INSTANCE}
    marker = gc.repo_path / MARKER_FILE
    if marker.is_file():
        offered |= {
            line.strip() for line in marker.read_text(encoding="utf-8").splitlines() if line.strip()
        }
    return offered


def seed_default_instances(gc: GitCrud, settings: Any) -> list[str]:
    """Write the profile's default app overlays that this deployment has not been offered.

    Returns the apps seeded, in the order they were written. One commit carries
    every overlay and the marker together: a marker recorded without its overlays
    would turn those apps off permanently, and overlays without the marker would
    be seeded again on the next start.
    """
    profile = (settings.deployment.profile or "").strip()
    if not profile:
        logger.info("Deployment states no profile, so no default app instances were seeded")
        return []

    offered = _already_offered(gc)
    artifacts: dict[str, str] = {}
    seeded: list[str] = []
    for service in default_services(profile):
        if service in offered:
            continue
        app = instances.instance_of(service, SEED_INSTANCE)
        overlay = yaml_dump_string(instances.initial_overlay(app))
        artifacts[instances.overlay_file(gc, app)] = overlay
        seeded.append(service)

    if not seeded:
        return []

    artifacts[MARKER_FILE] = "".join(f"{name}\n" for name in sorted(offered | set(seeded)))
    message = build_message(
        CommitContext(
            ctype="seed",
            scope=profile,
            summary="default app instances",
            actor=ENGINE_ACTOR,
            role="helmvars:write",
        )
    )
    gc.repo.publish(artifacts, message)
    return seeded
