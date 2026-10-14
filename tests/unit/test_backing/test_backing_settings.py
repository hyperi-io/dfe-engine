#  Project:      dfe-engine
#  File:         tests/unit/test_backing/test_backing_settings.py
#  Purpose:      The deployer's backing-service facts, read from the environment
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Six env vars carry who runs Kafka and ClickHouse, and one that must not.

``DFE_KAFKA_PROVIDER`` derives the engine's OWN broker protocol, and every
in-cluster deploy's listener differs from what a provider key derives, so the
facts travel under their own names and the two never mix.
"""

import os

import pytest

from dfe_engine.settings import (
    BackingFactSettings,
    BackingSettings,
    backing_from_environment,
    load_settings,
    reset_settings,
)

FACT_ENV = {
    "DFE_DEPLOYMENT_KAFKA_PROVIDER": ("kafka", "provider"),
    "DFE_DEPLOYMENT_KAFKA_TIER": ("kafka", "tier"),
    "DFE_DEPLOYMENT_KAFKA_REGION": ("kafka", "region"),
    "DFE_DEPLOYMENT_CLICKHOUSE_PROVIDER": ("clickhouse", "provider"),
    "DFE_DEPLOYMENT_CLICKHOUSE_TIER": ("clickhouse", "tier"),
    "DFE_DEPLOYMENT_CLICKHOUSE_REGION": ("clickhouse", "region"),
}


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch, tmp_path):
    """Strip every DFE_* var and run from an empty dir, so a developer .env cannot leak in."""
    for key in list(os.environ):
        if key.startswith(("DFE_", "CLICKHOUSE_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DFE_ENV", "test")
    monkeypatch.chdir(tmp_path)
    reset_settings()
    yield
    reset_settings()


def test_nothing_stated_is_empty_facts():
    backing = load_settings().deployment.backing

    assert backing == BackingSettings()
    assert backing.kafka == backing.clickhouse == BackingFactSettings()


@pytest.mark.parametrize(("env_var", "where"), FACT_ENV.items())
def test_each_env_var_lands_on_its_own_fact(monkeypatch, env_var, where):
    monkeypatch.setenv(env_var, " stated ")
    service, fact = where

    backing = load_settings().deployment.backing

    assert getattr(backing.facts(service), fact) == "stated"
    others = [
        getattr(backing.facts(s), f) for s, f in FACT_ENV.values() if (s, f) != (service, fact)
    ]
    assert others == [""] * (len(FACT_ENV) - 1)


def test_the_provider_key_is_lower_cased_and_the_tier_kept_as_given(monkeypatch):
    monkeypatch.setenv("DFE_DEPLOYMENT_KAFKA_PROVIDER", " Confluent-Cloud ")
    monkeypatch.setenv("DFE_DEPLOYMENT_KAFKA_TIER", "Express.M7g.Large")

    kafka = load_settings().deployment.backing.kafka

    assert kafka.provider == "confluent-cloud"
    assert kafka.tier == "Express.M7g.Large"


def test_the_engines_own_kafka_provider_never_becomes_a_backing_fact(monkeypatch):
    monkeypatch.setenv("DFE_KAFKA_PROVIDER", "strimzi")
    monkeypatch.setenv("DFE_KAFKA_SASL_USERNAME", "u")
    monkeypatch.setenv("DFE_KAFKA_SASL_PASSWORD", "p")

    settings = load_settings()

    assert settings.kafka.provider == "strimzi"
    assert settings.deployment.backing.kafka.provider == ""


def test_a_backing_fact_never_moves_the_engines_own_protocol(monkeypatch):
    """A managed broker's key derives SASL_SSL; the engine's in-cluster listener is not that."""
    monkeypatch.setenv("DFE_DEPLOYMENT_KAFKA_PROVIDER", "confluent-cloud")

    settings = load_settings()

    assert settings.kafka.provider == ""
    assert settings.kafka.security_protocol == "PLAINTEXT"


def test_an_unstated_service_answers_empty_facts():
    assert BackingSettings().facts("postgres") == BackingFactSettings()


def test_backing_from_environment_reads_what_load_settings_reads(monkeypatch):
    monkeypatch.setenv("DFE_DEPLOYMENT_CLICKHOUSE_PROVIDER", "clickhouse-cloud")
    monkeypatch.setenv("DFE_DEPLOYMENT_KAFKA_REGION", "eu-west-1")

    assert backing_from_environment() == load_settings().deployment.backing
    assert backing_from_environment().clickhouse.provider == "clickhouse-cloud"


def test_backing_from_environment_with_nothing_set_is_empty():
    assert backing_from_environment() == BackingSettings()
