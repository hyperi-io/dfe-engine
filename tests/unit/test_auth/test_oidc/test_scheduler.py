#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_scheduler.py
#  Purpose:      Unit tests for the background OIDC group sync scheduler
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Unit tests for the background OIDC group sync scheduler."""

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.scheduler import sync_is_due, sync_outcome
from tests.unit.test_auth.factories import (
    make_group_store,
    make_oidc_provider,
    make_oidc_provider_registry,
    make_oidc_sync_scheduler,
)
from tests.unit.test_auth.test_oidc.scheduler_cases import (
    NOW,
    SYNC_IS_DUE_CASES,
    SYNC_OUTCOME_CASES,
    SyncIsDueCase,
    SyncOutcomeCase,
)

DIRECTORY_ENV = "DFE_TEST_SCHEDULER_DIRECTORY"


def _mock_directory_provider(**kwargs: object) -> OIDCProvider:
    """An api-mode provider whose directory is the JSON fixture named by DIRECTORY_ENV."""
    groups = {"directory_backend": "mock", "mock_directory_env": DIRECTORY_ENV, "mode": "api"}
    return make_oidc_provider(groups=groups, **kwargs)


async def _wait_for(*, condition: Callable[[], bool]) -> bool:
    """Poll *condition* for up to five seconds and report whether it came true."""
    for _attempt in range(500):
        if condition():
            return True
        await asyncio.sleep(0.01)
    return False


def _write_directory(
    *, groups: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Write a mock directory fixture holding *groups* and point DIRECTORY_ENV at it."""
    path = tmp_path / "directory.json"
    path.write_text(json.dumps({"groups": groups}), encoding="utf-8")
    monkeypatch.setenv(DIRECTORY_ENV, str(path))


@pytest.fixture
def broken_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A directory whose one group description cannot be written back to YAML, so every run fails after the listing."""
    groups = [{"description": "a\x85b", "id": "g-ops", "name": "operators"}]
    _write_directory(groups=groups, monkeypatch=monkeypatch, tmp_path=tmp_path)


@pytest.fixture
def directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A directory holding one valid group and one whose name makes no group name."""
    groups = [{"id": "g-ops", "name": "operators"}, {"id": "g-bad", "name": "!!!"}]
    _write_directory(groups=groups, monkeypatch=monkeypatch, tmp_path=tmp_path)


@pytest.fixture
def group_store(tmp_path: Path) -> GroupStore:
    """A group store on disk under the test's own directory."""
    return make_group_store(directory=tmp_path / "groups")


@pytest.fixture
def registry(tmp_path: Path) -> OIDCProviderRegistry:
    """A provider registry on disk under the test's own directory."""
    return make_oidc_provider_registry(directory=tmp_path / "oidc-providers")


class TestSyncIsDue:
    @pytest.mark.parametrize(
        "case", SYNC_IS_DUE_CASES, ids=[case["id"] for case in SYNC_IS_DUE_CASES]
    )
    def test_matches_expected(self, case: SyncIsDueCase):
        assert sync_is_due(now=NOW, provider=case["provider"]) == case["expected_due"]


class TestSyncOutcome:
    @pytest.mark.parametrize(
        "case", SYNC_OUTCOME_CASES, ids=[case["id"] for case in SYNC_OUTCOME_CASES]
    )
    def test_matches_expected(self, case: SyncOutcomeCase):
        assert sync_outcome(result=case["result"]) == case["expected_outcome"]


class TestOidcSyncScheduler:
    async def test_run_forever(
        self, directory: None, group_store: GroupStore, registry: OIDCProviderRegistry
    ):
        def synced() -> bool:
            return group_store.get(name="operators") is not None

        registry.create(name="mock-dir", provider=_mock_directory_provider())
        scheduler = make_oidc_sync_scheduler(
            group_store=group_store, registry=registry, tick_seconds=0.01
        )
        task = asyncio.create_task(scheduler.run_forever())
        ran = await _wait_for(condition=synced)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ran is True

    async def test_run_forever_survives_a_failing_tick(
        self,
        directory: None,
        group_store: GroupStore,
        registry: OIDCProviderRegistry,
        tmp_path: Path,
    ):
        def synced() -> bool:
            return group_store.get(name="operators") is not None

        # A provider file that does not validate makes every registry listing raise.
        broken = tmp_path / "oidc-providers" / "broken.yaml"
        broken.write_text("type: not-a-provider-type\n", encoding="utf-8")
        scheduler = make_oidc_sync_scheduler(
            group_store=group_store, registry=registry, tick_seconds=0.01
        )
        task = asyncio.create_task(scheduler.run_forever())
        await asyncio.sleep(0.1)
        broken.unlink()
        registry.create(name="mock-dir", provider=_mock_directory_provider())
        ran = await _wait_for(condition=synced)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ran is True

    class TestRunOnce:
        async def test_syncs_a_due_provider(
            self, directory: None, group_store: GroupStore, registry: OIDCProviderRegistry
        ):
            def record() -> None:
                reconciles.append("requested")

            reconciles = []
            registry.create(name="mock-dir", provider=_mock_directory_provider())
            scheduler = make_oidc_sync_scheduler(
                group_store=group_store, on_groups_created=record, registry=registry
            )

            outcomes = await scheduler.run_once(now=NOW)

            assert (outcomes, group_store.get(name="operators") is not None, reconciles) == (
                {"mock-dir": "partial"},
                True,
                ["requested"],
            )

        async def test_leaves_a_provider_that_is_not_due(
            self, directory: None, group_store: GroupStore, registry: OIDCProviderRegistry
        ):
            registry.create(
                name="mock-dir", provider=_mock_directory_provider(last_sync_at=NOW.isoformat())
            )
            scheduler = make_oidc_sync_scheduler(group_store=group_store, registry=registry)

            outcomes = await scheduler.run_once(now=NOW)

            assert (outcomes, group_store.get(name="operators")) == ({}, None)

        async def test_holds_a_failed_provider_back_for_its_interval(
            self, broken_directory: None, group_store: GroupStore, registry: OIDCProviderRegistry
        ):
            registry.create(name="mock-dir", provider=_mock_directory_provider())
            scheduler = make_oidc_sync_scheduler(group_store=group_store, registry=registry)

            first = await scheduler.run_once(now=NOW)
            next_tick = await scheduler.run_once(now=NOW + timedelta(minutes=1))
            next_interval = await scheduler.run_once(now=NOW + timedelta(hours=1))

            assert (first, next_tick, next_interval) == (
                {"mock-dir": "error"},
                {},
                {"mock-dir": "error"},
            )
