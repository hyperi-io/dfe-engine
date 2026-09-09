#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/conftest.py
#  Purpose:      Shared fixtures for the app-management library tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Fixtures backing the app-management tests with a real local git repo.

The settings are the REAL settings object rather than a stub: the compilers ask
it which transports the deployment offers, and a stub would answer whatever the
test wished for.
"""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import DFESettings
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceNotFoundError


@pytest.fixture
def crud(tmp_path):
    """GitCrud over a fresh local (no-remote) deploy repo."""
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


def deployment(**transport) -> DFESettings:
    """Settings differing from the defaults only in the transport block."""
    return DFESettings(env="dev", transport=transport)


@pytest.fixture
def settings() -> DFESettings:
    """A deployment running a bus, which is the default posture."""
    return deployment(default="bus")


@pytest.fixture
def direct_settings() -> DFESettings:
    """A brokerless deployment: every source runs point to point."""
    return deployment(default="direct", bus_present=False)


class FakeRegistry:
    """The two reader methods the routing compilers call."""

    def __init__(self, sources: list[Source]) -> None:
        self._sources = {s.source: s for s in sources}

    def get_source(self, source_name: str) -> Source:
        if source_name not in self._sources:
            raise SourceNotFoundError(f"Source {source_name!r} not found")
        return self._sources[source_name]

    def get_all_sources(
        self, enabled_only: bool = False, *, states: tuple[str, ...] | None = None
    ) -> list[Source]:
        sources = list(self._sources.values())
        if states is not None:
            return [s for s in sources if s.state in states]
        if enabled_only:
            return [s for s in sources if s.enabled]
        return sources
