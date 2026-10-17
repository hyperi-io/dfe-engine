#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_bootstrap_sources.py
#  Purpose:      Startup brings deployed source tables to the env default TTL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The bootstrap reconciles source TTLs after the schema phase, and never fails on them."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

import dfe_engine.clickhouse.bootstrap as bootstrap_module
import dfe_engine.schema.retention as retention_module
from dfe_engine.clickhouse.bootstrap import bootstrap_clickhouse
from dfe_engine.schema import phase
from dfe_engine.schema.phase import STATE_CONVERGED, SchemaBootstrapState
from dfe_engine.schema.retention import reconcile_source_ttls
from dfe_engine.settings import ClickHouseSettings, DFESettings
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceRegistryError

GUARD_REFUSAL = "unit tests do not reconcile against a ClickHouse (conftest guard)"
NOT_REBUILT = "all_sources not rebuilt"
TTL_FAILED = "source TTL reconcile failed"
UNREADABLE = "the sources directory could not be read"

# The opening of every line either source pass logs.
SOURCE_PASS_EVENTS = ("all_sources", "bootstrap sources", "source TTL reconcile")


class _UnreachableClient:
    """A server every read fails against."""

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        raise ConnectionError("clickhouse unreachable")

    def command(self, sql: str) -> None:
        raise ConnectionError("clickhouse unreachable")


class _DeployStore:
    """Every source is deployed at 1.0.0."""

    def load_deploy_document(self, source_name: str) -> Any:
        return SimpleNamespace(deployed_version="1.0.0")


def _rebuilds(*, events: list[dict]) -> list[dict]:
    """The all_sources rebuild failures logged, with their fields."""
    return [entry for entry in events if entry["event"] == NOT_REBUILT]


def _registry_of(*, names: list[str]) -> Callable[[], list[Source]]:
    """A source loader answering as a registry holding a source for each of *names* does."""

    def load() -> list[Source]:
        """Return one source per name."""
        return [_source(name=name) for name in names]

    return load


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        clickhouse=SimpleNamespace(
            bootstrap_tables=True,
            default_engine="MergeTree",
            default_table_profile="timeseries",
            default_ttl_days=0,
            effective_data_database="dfe",
            landing_table="main",
            topology="single",
        ),
        schemas=SimpleNamespace(schemas_dir=""),
    )


def _source(*, name: str, resource_type: str = "custom") -> Source:
    return Source.model_validate(
        {
            "match": {"field": "f", "value": name},
            "resource_type": resource_type,
            "source": name,
        }
    )


def _source_passes(*, events: list[dict]) -> list[dict]:
    """The lines the TTL reconcile and the all_sources rebuild logged, with their fields."""
    return [entry for entry in events if entry["event"].startswith(SOURCE_PASS_EVENTS)]


def _unreadable_registry() -> list[Source]:
    """Fail as a registry whose store cannot be read does."""
    raise SourceRegistryError(UNREADABLE)


@pytest.fixture
def settings() -> DFESettings:
    """The deployment's default settings."""
    return DFESettings(env="dev")


@pytest.fixture
def switched_off() -> Iterator[DFESettings]:
    """Settings with the schema phase switched off, restoring the phase's process-wide state after the test."""
    before = phase.current_state()
    yield DFESettings(clickhouse=ClickHouseSettings(bootstrap_tables=False), env="dev")
    phase._set_state(before)


@pytest.fixture
def boot(monkeypatch):
    """A bootstrap whose schema phase converges without a server; records the reconcile."""

    def ignore(*args, **kwargs):
        return None

    def fake_client_manager(config):
        return SimpleNamespace(get_clickhouse_client=unreachable)

    def unreachable():
        return _UnreachableClient()

    def converged(*args, **kwargs):
        return SchemaBootstrapState(state=STATE_CONVERGED)

    monkeypatch.setattr(bootstrap_module, "run_bootstrap", converged)
    monkeypatch.setattr(bootstrap_module, "get_clickhouse_config", ignore)
    monkeypatch.setattr(
        bootstrap_module.ClickHouseManager, "get_instance", staticmethod(fake_client_manager)
    )
    return monkeypatch


def test_a_failing_source_reconcile_never_fails_the_bootstrap(boot, settings: DFESettings):
    def exploding_reconcile(client, *, settings, sources):
        raise RuntimeError("boom")

    boot.setattr(retention_module, "reconcile_source_ttls", exploding_reconcile)

    state = bootstrap_clickhouse(load_sources=_registry_of(names=["okta"]), settings=settings)
    assert state.converged is True


def test_the_bootstrap_hands_every_source_to_the_reconcile(boot, settings: DFESettings):
    def recording_reconcile(client, *, settings, sources):
        seen.extend(source.source for source in sources)
        return SimpleNamespace(report=None, sources_skipped=0)

    seen = []
    boot.setattr(retention_module, "reconcile_source_ttls", recording_reconcile)

    bootstrap_clickhouse(load_sources=_registry_of(names=["okta", "zeek"]), settings=settings)

    assert seen == ["okta", "zeek"]


def test_an_unreachable_table_is_skipped_and_core_sources_are_left_alone(monkeypatch):
    def deploy_store(settings):
        return _DeployStore()

    monkeypatch.setattr(retention_module.SourceDeploymentStore, "from_settings", deploy_store)
    sources = [_source(name="okta"), _source(name="main", resource_type="core")]

    outcome = reconcile_source_ttls(_UnreachableClient(), settings=_settings(), sources=sources)

    assert outcome.sources_skipped == 1
    assert outcome.sources_reconciled == 0


class TestBootstrapClickhouse:
    # The conftest guard refuses the admin client, so an attempted rebuild logs the refusal.
    def test_rebuilds_all_sources_with_the_phase_switched_off(
        self, audit_events: list[dict], switched_off: DFESettings
    ):
        state = bootstrap_clickhouse(load_sources=_registry_of(names=[]), settings=switched_off)

        assert (state.state, _rebuilds(events=audit_events)) == (
            "unknown",
            [{"event": NOT_REBUILT, "changed": [], "database": "dfe", "error": GUARD_REFUSAL}],
        )

    def test_no_loader_runs_no_source_pass(
        self, audit_events: list[dict], boot: pytest.MonkeyPatch, settings: DFESettings
    ):
        state = bootstrap_clickhouse(load_sources=None, settings=settings)

        assert (state.converged, _source_passes(events=audit_events)) == (True, [])

    def test_an_empty_registry_attempts_only_the_rebuild(
        self, audit_events: list[dict], boot: pytest.MonkeyPatch, settings: DFESettings
    ):
        bootstrap_clickhouse(load_sources=_registry_of(names=[]), settings=settings)

        assert _source_passes(events=audit_events) == [
            {"event": NOT_REBUILT, "changed": [], "database": "dfe", "error": GUARD_REFUSAL}
        ]

    def test_an_unreadable_registry_fails_only_the_source_passes(
        self, audit_events: list[dict], boot: pytest.MonkeyPatch, settings: DFESettings
    ):
        state = bootstrap_clickhouse(load_sources=_unreadable_registry, settings=settings)

        assert (state.converged, _source_passes(events=audit_events)) == (
            True,
            [
                {"event": TTL_FAILED, "error": UNREADABLE},
                {"event": NOT_REBUILT, "changed": [], "database": "dfe", "error": GUARD_REFUSAL},
            ],
        )
