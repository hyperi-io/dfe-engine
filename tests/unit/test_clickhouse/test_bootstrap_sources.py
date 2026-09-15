#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_bootstrap_sources.py
#  Purpose:      Startup brings deployed source tables to the env default TTL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The bootstrap reconciles source TTLs after the core tables, and never fails on them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import dfe_engine.clickhouse.bootstrap as bootstrap_module
import dfe_engine.schema.retention as retention_module
from dfe_engine.clickhouse.bootstrap import bootstrap_clickhouse
from dfe_engine.schema.retention import reconcile_source_ttls
from dfe_engine.source.models import Source


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


@pytest.fixture
def boot(monkeypatch):
    """A bootstrap whose core apply succeeds without a server; records what it reconciled."""

    def ignore(*args, **kwargs):
        return None

    def fake_client_manager(config):
        return SimpleNamespace(get_clickhouse_client=unreachable)

    def unreachable():
        return _UnreachableClient()

    monkeypatch.setattr(bootstrap_module, "apply_core_schema", ignore)
    monkeypatch.setattr(bootstrap_module, "apply_query_log_archive", ignore)
    monkeypatch.setattr(bootstrap_module, "log_report", ignore)
    monkeypatch.setattr(bootstrap_module, "get_clickhouse_config", ignore)
    monkeypatch.setattr(
        bootstrap_module.ClickHouseManager, "get_instance", staticmethod(fake_client_manager)
    )
    return monkeypatch


def test_a_failing_source_reconcile_never_fails_the_bootstrap(boot):
    def exploding_reconcile(client, *, settings, sources):
        raise RuntimeError("boom")

    boot.setattr(retention_module, "reconcile_source_ttls", exploding_reconcile)

    assert bootstrap_clickhouse(settings=_settings(), sources=[_source(name="okta")]) is True


def test_the_bootstrap_hands_every_source_to_the_reconcile(boot):
    def recording_reconcile(client, *, settings, sources):
        seen.extend(source.source for source in sources)
        return SimpleNamespace(report=None, sources_skipped=0)

    seen = []
    boot.setattr(retention_module, "reconcile_source_ttls", recording_reconcile)

    bootstrap_clickhouse(settings=_settings(), sources=[_source(name="okta"), _source(name="zeek")])

    assert seen == ["okta", "zeek"]


def test_an_unreachable_table_is_skipped_and_core_sources_are_left_alone(monkeypatch):
    def deploy_store(settings):
        return _DeployStore()

    monkeypatch.setattr(retention_module.SourceDeploymentStore, "from_settings", deploy_store)
    sources = [_source(name="okta"), _source(name="main", resource_type="core")]

    outcome = reconcile_source_ttls(_UnreachableClient(), settings=_settings(), sources=sources)

    assert outcome.sources_skipped == 1
    assert outcome.sources_reconciled == 0
