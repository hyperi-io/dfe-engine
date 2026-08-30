#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/conftest.py
#  Purpose:      Shared fixtures for the app-management library tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Fixtures backing the app-management tests with a real local git repo."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitops.repo import GitopsRepo


@pytest.fixture
def crud(tmp_path):
    """GitCrud over a fresh local (no-remote) deploy repo."""
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())
