"""Kafka credential contract - provider->mechanism derivation (dfe-engine#98).

Checkpoint 1: the derivation table is the SSoT and is MIRRORED in scalo-rs (Rust).
CANONICAL_TABLE below is the exact cross-language expectation - a Rust reviewer
cross-checks scalo's table against this. No broker is touched (pure derivation).
"""

import os

import pytest
from pydantic import ValidationError

from dfe_engine.kafka.contract import (
    KafkaContractError,
    derive,
    validate,
)
from dfe_engine.settings import KafkaSettings, _get_env_overrides

# The cross-language contract. provider -> (security_protocol, sasl_mechanism).
# scalo-rs A1 MUST produce this exact table. Change here + #98 + Rust together.
CANONICAL_TABLE = {
    "strimzi": ("SASL_SSL", "SCRAM-SHA-512"),
    "redpanda": ("SASL_SSL", "SCRAM-SHA-512"),
    "msk": ("SASL_SSL", "SCRAM-SHA-512"),
    "redpanda-cloud": ("SASL_SSL", "SCRAM-SHA-512"),
    "confluent-cloud": ("SASL_SSL", "PLAIN"),
    "plaintext": ("PLAINTEXT", ""),
    "msk_iam": ("SASL_SSL", "OAUTHBEARER"),
}


class TestDerive:
    @pytest.mark.parametrize(("provider", "expected"), list(CANONICAL_TABLE.items()))
    def test_table(self, provider, expected):
        assert derive(provider) == expected

    def test_owned_brokers_are_scram(self):
        # DFE controls these brokers -- SCRAM-512, never weakened to PLAIN.
        for provider in ("strimzi", "redpanda", "msk"):
            assert derive(provider) == ("SASL_SSL", "SCRAM-SHA-512")

    def test_confluent_is_the_only_plain(self):
        # PLAIN is the single sanctioned fallback, and only Confluent Cloud needs it.
        plain = [p for p, (_, m) in CANONICAL_TABLE.items() if m == "PLAIN"]
        assert plain == ["confluent-cloud"]

    def test_unknown_provider_raises(self):
        with pytest.raises(KafkaContractError, match="unknown kafka provider"):
            derive("kinesis")


class TestValidate:
    def test_plain_over_plaintext_refused(self):
        # The one hard floor: PLAIN creds must never ride a plaintext transport.
        with pytest.raises(KafkaContractError, match="SASL_SSL"):
            validate(security_protocol="PLAINTEXT", sasl_mechanism="PLAIN")
        with pytest.raises(KafkaContractError, match="SASL_SSL"):
            validate(security_protocol="SASL_PLAINTEXT", sasl_mechanism="PLAIN")

    def test_plain_over_tls_ok(self):
        validate(security_protocol="SASL_SSL", sasl_mechanism="PLAIN")

    def test_sasl_ssl_needs_a_mechanism(self):
        with pytest.raises(KafkaContractError, match=r"requires a sasl\.mechanism"):
            validate(security_protocol="SASL_SSL", sasl_mechanism="")

    def test_plaintext_dev_ok(self):
        validate(security_protocol="PLAINTEXT", sasl_mechanism="")

    def test_every_derived_pair_passes_validation(self):
        # Derivation must never emit a config its own validator would reject.
        for provider in CANONICAL_TABLE:
            proto, mech = derive(provider)
            validate(security_protocol=proto, sasl_mechanism=mech)


class TestKafkaSettings:
    def test_provider_derives_confluent_plain(self):
        s = KafkaSettings(provider="confluent-cloud")
        assert s.security_protocol == "SASL_SSL"
        assert s.sasl_mechanism == "PLAIN"

    def test_provider_derives_owned_scram(self):
        s = KafkaSettings(provider="strimzi")
        assert (s.security_protocol, s.sasl_mechanism) == ("SASL_SSL", "SCRAM-SHA-512")

    def test_provider_overrides_hand_set_mechanism(self):
        # provider wins -- a hand-set mechanism is ignored (contract: never hand-set).
        s = KafkaSettings(provider="strimzi", sasl_mechanism="PLAIN")
        assert s.sasl_mechanism == "SCRAM-SHA-512"

    def test_default_is_plaintext_dev(self):
        s = KafkaSettings()
        assert (s.security_protocol, s.sasl_mechanism) == ("PLAINTEXT", "")

    def test_security_floor_applies_without_provider(self):
        # Even on the transition (no provider), PLAIN-over-plaintext is refused.
        # Pydantic wraps the KafkaContractError into a ValidationError; the
        # contract message ("SASL_SSL") still carries through.
        with pytest.raises(ValidationError, match="SASL_SSL"):
            KafkaSettings(security_protocol="SASL_PLAINTEXT", sasl_mechanism="PLAIN")

    def test_hand_set_scram_over_tls_still_ok(self):
        # Back-compat: an existing config that hand-set SCRAM+SASL_SSL keeps working.
        s = KafkaSettings(security_protocol="SASL_SSL", sasl_mechanism="SCRAM-SHA-512")
        assert (s.security_protocol, s.sasl_mechanism) == ("SASL_SSL", "SCRAM-SHA-512")


class TestEnvWiring:
    """DFE_KAFKA_PROVIDER binds to the override (the deploy path): deploys set the
    provider, and the mechanism is derived - never set via env."""

    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch):
        # Strip a populated developer .env so the binding is tested in isolation.
        for key in list(os.environ):
            if key.startswith(("DFE_", "KAFKA_")):
                monkeypatch.delenv(key, raising=False)

    def test_provider_env_binds_to_override(self, monkeypatch):
        monkeypatch.setenv("DFE_KAFKA_PROVIDER", "redpanda")
        overrides = _get_env_overrides()
        assert overrides["kafka"]["provider"] == "redpanda"
        # ... and the derivation turns that into SCRAM at construction.
        assert KafkaSettings(**overrides["kafka"]).sasl_mechanism == "SCRAM-SHA-512"


class TestTopicEnvWiring:
    """The topic-creation dials bind to env. The Kafka-less profile (receiver ->
    loader over direct gRPC) has no broker, so it must be able to turn the topic
    step off from the deploy that configures it."""

    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch):
        for key in list(os.environ):
            if key.startswith(("DFE_", "KAFKA_")):
                monkeypatch.delenv(key, raising=False)

    def test_defaults_when_unset(self):
        # No topic env set, so the section is pruned entirely and the model defaults stand.
        assert "kafka" not in _get_env_overrides()
        ks = KafkaSettings()
        assert ks.ensure_topics is True
        assert (ks.topic_partitions, ks.topic_replication_factor) == (3, 1)

    @pytest.mark.parametrize("raw", ["false", "False", "0", "no", "anything-not-truthy"])
    def test_ensure_topics_turns_off(self, monkeypatch, raw):
        monkeypatch.setenv("DFE_KAFKA_ENSURE_TOPICS", raw)
        assert KafkaSettings(**_get_env_overrides()["kafka"]).ensure_topics is False

    @pytest.mark.parametrize("raw", ["true", "True", "1", "yes"])
    def test_ensure_topics_turns_on(self, monkeypatch, raw):
        monkeypatch.setenv("DFE_KAFKA_ENSURE_TOPICS", raw)
        assert KafkaSettings(**_get_env_overrides()["kafka"]).ensure_topics is True

    def test_partitions_and_replication_bind(self, monkeypatch):
        monkeypatch.setenv("DFE_KAFKA_TOPIC_PARTITIONS", "6")
        monkeypatch.setenv("DFE_KAFKA_TOPIC_REPLICATION_FACTOR", "3")
        ks = KafkaSettings(**_get_env_overrides()["kafka"])
        assert (ks.topic_partitions, ks.topic_replication_factor) == (6, 3)
