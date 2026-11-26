#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/factories.py
#  Purpose:      Factories for the org unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Factories for the org unit tests."""

from pathlib import Path

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.orgs.home import HomeOrgStore
from dfe_engine.orgs.models import Org
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.orgs.tenancy import Tenancy


def make_gitcrud(*, path: Path) -> GitCrud:
    """Build a gitcrud.GitCrud over a local deploy repo that never pushes."""
    return GitCrud(registry=default_registry(), repo=GitopsRepo(local_path=str(path), push=False))


def make_home_org_store(*, config_dir: Path, crud: GitCrud | None = None) -> HomeOrgStore:
    """Build an orgs.home.HomeOrgStore."""
    return HomeOrgStore(config_dir=config_dir, crud=crud)


def make_org(name: str, **kwargs: object) -> Org:
    """Build an orgs.models.Org."""
    return Org.model_validate({"name": name, **kwargs})


def make_org_registry(*, home: HomeOrgStore | None, path: Path) -> OrgRegistry:
    """Build an orgs.registry.OrgRegistry naming your org through *home*."""
    registry = OrgRegistry(path)
    registry.home = home
    return registry


def make_tenancy(*, orgs: tuple[Org, ...] = (), home: str | None = None) -> Tenancy:
    """Build an orgs.tenancy.Tenancy naming *home* as your org."""
    return Tenancy(orgs=orgs, home=home)
