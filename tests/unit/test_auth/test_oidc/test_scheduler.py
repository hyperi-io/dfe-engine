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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.scheduler import sync_is_due
from tests.support.failing_stores import StampFailingProviderRegistry
from tests.unit.test_auth.factories import (
    make_group_store,
    make_oidc_provider,
    make_oidc_provider_registry,
    make_oidc_sync_scheduler,
)
from tests.unit.test_auth.test_oidc.scheduler_cases import (
    NOW,
    SYNC_IS_DUE_CASES,
    SyncIsDueCase,
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

    async def test_a_failing_tick_logs_the_failures_class_not_its_text(
        self,
        audit_events: list[dict],
        directory: None,
        group_store: GroupStore,
        registry: OIDCProviderRegistry,
    ):
        """A tick fails here when the groups-created hook raises, and the text it carries stays out of the log."""

        def failed() -> bool:
            return any(
                e["event"].startswith("OIDC scheduled group sync tick failed") for e in audit_events
            )

        def refuse() -> None:
            raise RuntimeError("tick-sentinel-7731")

        registry.create(name="mock-dir", provider=_mock_directory_provider())
        scheduler = make_oidc_sync_scheduler(
            group_store=group_store, on_groups_created=refuse, registry=registry, tick_seconds=0.01
        )
        task = asyncio.create_task(scheduler.run_forever())
        logged = await _wait_for(condition=failed)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert logged is True
        (line,) = [
            e
            for e in audit_events
            if e["event"].startswith("OIDC scheduled group sync tick failed")
        ]
        assert "exception" not in line
        assert line["error_type"] == "RuntimeError"
        assert "tick-sentinel-7731" not in json.dumps(audit_events, default=str)

    async def test_run_forever_survives_a_failing_tick(
        self,
        directory: None,
        group_store: GroupStore,
        monkeypatch: pytest.MonkeyPatch,
        registry: OIDCProviderRegistry,
        tmp_path: Path,
    ):
        def synced() -> bool:
            return group_store.get(name="viewers") is not None

        def refuse_once() -> None:
            calls.append("created")
            if len(calls) == 1:
                raise RuntimeError("the hook failed")

        calls: list[str] = []
        # The first provider's sync creates groups and the hook raises, which fails that tick.
        registry.create(name="mock-dir", provider=_mock_directory_provider())
        second = tmp_path / "second.json"
        second.write_text(
            json.dumps({"groups": [{"id": "g-v", "name": "viewers"}]}), encoding="utf-8"
        )
        monkeypatch.setenv(f"{DIRECTORY_ENV}_SECOND", str(second))
        scheduler = make_oidc_sync_scheduler(
            group_store=group_store,
            on_groups_created=refuse_once,
            registry=registry,
            tick_seconds=0.01,
        )
        task = asyncio.create_task(scheduler.run_forever())
        await _wait_for(condition=lambda: len(calls) == 1)
        registry.create(
            name="second-dir",
            provider=make_oidc_provider(
                groups={
                    "directory_backend": "mock",
                    "mock_directory_env": f"{DIRECTORY_ENV}_SECOND",
                    "mode": "api",
                }
            ),
        )
        ran = await _wait_for(condition=synced)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (ran, calls) == (True, ["created", "created"])

    async def test_a_provider_file_that_does_not_load_leaves_the_others_syncing(
        self,
        directory: None,
        group_store: GroupStore,
        registry: OIDCProviderRegistry,
        tmp_path: Path,
    ):
        """One malformed file once failed every tick for every provider."""
        registry.create(name="mock-dir", provider=_mock_directory_provider())
        (tmp_path / "oidc-providers" / "broken.yaml").write_text(
            "type: not-a-provider-type\n", encoding="utf-8"
        )
        scheduler = make_oidc_sync_scheduler(group_store=group_store, registry=registry)

        outcomes = await scheduler.run_once(now=NOW)

        assert (outcomes, group_store.get(name="operators") is not None) == (
            {"mock-dir": "partial"},
            True,
        )

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

        async def test_a_provider_with_no_credential_waits_out_its_interval(
            self, group_store: GroupStore, registry: OIDCProviderRegistry
        ):
            unconfigured = make_oidc_provider(
                type="okta", groups={"mode": "api", "okta_domain": "example.okta.com"}
            )
            registry.create(name="no-token", provider=unconfigured)
            scheduler = make_oidc_sync_scheduler(group_store=group_store, registry=registry)
            # The sync stamps last_sync_at from the real clock, so the ticks count from it.
            start = datetime.now(UTC)

            first = await scheduler.run_once(now=start)
            next_tick = await scheduler.run_once(now=start + timedelta(minutes=1))
            next_interval = await scheduler.run_once(now=start + timedelta(hours=2))

            stored = registry.get("no-token")
            assert stored is not None
            assert (first, next_tick, next_interval) == (
                {"no-token": "not_configured"},
                {},
                {"no-token": "not_configured"},
            )
            assert stored.last_sync_status == "not_configured"

        async def test_holds_a_failed_provider_back_for_its_interval(
            self, directory: None, group_store: GroupStore, tmp_path: Path
        ):
            """A run that cannot stamp the provider leaves it due, so the scheduler holds it back."""
            registry = StampFailingProviderRegistry(tmp_path / "unstampable")
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

        async def test_a_failed_run_logs_the_failures_class_not_its_text(
            self,
            audit_events: list[dict],
            directory: None,
            group_store: GroupStore,
            tmp_path: Path,
        ):
            registry = StampFailingProviderRegistry(tmp_path / "unstampable")
            registry.create(name="mock-dir", provider=_mock_directory_provider())
            scheduler = make_oidc_sync_scheduler(group_store=group_store, registry=registry)

            await scheduler.run_once(now=NOW)

            (line,) = [e for e in audit_events if e["event"] == "OIDC scheduled group sync failing"]
            assert "error" not in line
            assert line["error_type"] == "OSError"
            assert "No space left" not in json.dumps(line, default=str)

        async def test_a_group_the_store_cannot_write_leaves_the_provider_stamped_and_partial(
            self, broken_directory: None, group_store: GroupStore, registry: OIDCProviderRegistry
        ):
            registry.create(name="mock-dir", provider=_mock_directory_provider())
            scheduler = make_oidc_sync_scheduler(group_store=group_store, registry=registry)
            # The sync stamps last_sync_at from the real clock, so the ticks count from it.
            start = datetime.now(UTC)

            first = await scheduler.run_once(now=start)
            next_tick = await scheduler.run_once(now=start + timedelta(minutes=1))

            stored = registry.get("mock-dir")
            assert stored is not None
            assert (first, next_tick) == ({"mock-dir": "partial"}, {})
            assert stored.last_sync_at
            assert stored.last_sync_status.startswith("partial: 1 of 1 groups skipped")

        async def test_a_google_provider_with_no_service_account_is_its_own_outcome(
            self, group_store: GroupStore, registry: OIDCProviderRegistry
        ):
            google = make_oidc_provider(
                type="google", groups={"enrich_on_login": True, "mode": "api"}
            )
            registry.create(name="google", provider=google)
            scheduler = make_oidc_sync_scheduler(group_store=group_store, registry=registry)

            outcomes = await scheduler.run_once(now=datetime.now(UTC))

            assert outcomes == {"google": "user_token"}

        async def test_a_scim_group_bound_to_the_provider_is_synced(
            self,
            group_store: GroupStore,
            monkeypatch: pytest.MonkeyPatch,
            registry: OIDCProviderRegistry,
            tmp_path: Path,
        ):
            groups = [{"description": "From the directory", "id": "g-ops", "name": "operators"}]
            _write_directory(groups=groups, monkeypatch=monkeypatch, tmp_path=tmp_path)
            group_store.create(
                "operators", roles=["data_analyst"], source_provider="scim", source_id="g-ops"
            )
            registry.create(name="mock-dir", provider=_mock_directory_provider())
            scheduler = make_oidc_sync_scheduler(
                bindings={"scim": "mock-dir"}, group_store=group_store, registry=registry
            )

            outcomes = await scheduler.run_once(now=NOW)

            stored = group_store.get("operators")
            assert outcomes == {"mock-dir": "ok"}
            assert (stored.source_provider, stored.description) == ("scim", "From the directory")
