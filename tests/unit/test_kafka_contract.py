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
    "strimzi-no-tls": ("SASL_PLAINTEXT", "SCRAM-SHA-512"),
    "redpanda-no-tls": ("SASL_PLAINTEXT", "SCRAM-SHA-512"),
    "msk": ("SASL_SSL", "SCRAM-SHA-512"),
    "redpanda-cloud": ("SASL_SSL", "SCRAM-SHA-512"),
    "confluent-cloud": ("SASL_SSL", "PLAIN"),
    "plaintext": ("PLAINTEXT", ""),
    "msk_iam": ("SASL_SSL", "OAUTHBEARER"),
}

# The two `-no-tls` keys the charts already use for a DFE-owned broker on its
# TLS-off in-cluster listener. dfe-infra's own table carries them
# (helm/library/dfe-common/templates/_kafka.tpl); scalo-rs and scalo-py do not yet.
NO_TLS_PROVIDERS = ("strimzi-no-tls", "redpanda-no-tls")


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


class TestTheInClusterPlaintextListener:
    """dfe-engine#332: one provider key names one LISTENER, not a cluster.

    The Strimzi the deploy charts stand up runs SASL on a TLS-off listener and
    every profile points the apps at it. Named `strimzi`, the engine derived
    SASL_SSL and every topic call came back "Failed to get metadata: Local:
    Broker transport failure".
    """

    @pytest.mark.parametrize("provider", NO_TLS_PROVIDERS)
    def test_it_is_sasl_over_a_plaintext_transport(self, provider):
        assert derive(provider) == ("SASL_PLAINTEXT", "SCRAM-SHA-512")

    @pytest.mark.parametrize("provider", NO_TLS_PROVIDERS)
    def test_only_the_transport_moves(self, provider):
        # A plaintext listener is never an excuse to weaken SCRAM to PLAIN.
        tls_key = provider.removesuffix("-no-tls")
        assert derive(provider)[1] == CANONICAL_TABLE[tls_key][1]

    @pytest.mark.parametrize("provider", NO_TLS_PROVIDERS)
    def test_the_pair_passes_validation(self, provider):
        protocol, mechanism = derive(provider)
        validate(security_protocol=protocol, sasl_mechanism=mechanism)

    def test_no_managed_provider_has_a_plaintext_key(self):
        # These two are the DFE-owned brokers only: a managed platform offers no
        # plaintext listener, and a key for one would invite a real downgrade.
        assert set(NO_TLS_PROVIDERS) == {p for p in CANONICAL_TABLE if p.endswith("-no-tls")}


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

    def test_a_bearer_token_over_plaintext_is_refused(self):
        # OAUTHBEARER puts the token itself on the wire, same as PLAIN.
        with pytest.raises(KafkaContractError, match="SASL_SSL"):
            validate(security_protocol="SASL_PLAINTEXT", sasl_mechanism="OAUTHBEARER")

    def test_scram_over_a_plaintext_listener_is_allowed(self):
        # The challenge-response never sends the password, which is what makes the
        # in-cluster plaintext listener a sanctioned case (dfe-engine#332).
        validate(security_protocol="SASL_PLAINTEXT", sasl_mechanism="SCRAM-SHA-512")

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

    def test_the_in_cluster_listener_derives_sasl_plaintext(self):
        # The whole of #332: the :9092 listener every profile points the apps at
        # derives SASL_PLAINTEXT, not the SASL_SSL that could not connect.
        s = KafkaSettings(provider="strimzi-no-tls")
        assert (s.security_protocol, s.sasl_mechanism) == ("SASL_PLAINTEXT", "SCRAM-SHA-512")


class TestTopicConfig:
    """The alterable topic configs DFE asks for, off unless a dial is set."""

    def _config(self, **kafka) -> dict[str, str]:
        from types import SimpleNamespace

        from dfe_engine.kafka.topics import deployment_topic_config

        return deployment_topic_config(SimpleNamespace(kafka=KafkaSettings(**kafka)))

    def test_nothing_set_asks_only_for_producer_compression(self):
        # An untouched deployment keeps the broker's own retention, which is what
        # every topic it already has carries, and stores batches as produced.
        assert self._config() == {"compression.type": "producer"}

    def test_retention_and_cleanup_reach_the_config(self):
        assert self._config(topic_retention_ms=86400000, topic_cleanup_policy="compact") == {
            "retention.ms": "86400000",
            "cleanup.policy": "compact",
            "compression.type": "producer",
        }

    def test_infinite_retention_is_expressible(self):
        assert self._config(topic_retention_ms=-1)["retention.ms"] == "-1"

    def test_compression_type_is_a_dial(self):
        assert self._config(topic_compression_type="zstd") == {"compression.type": "zstd"}

    def test_empty_compression_type_leaves_the_broker_default(self):
        assert self._config(topic_compression_type="") == {}


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

    def test_the_in_cluster_listener_key_binds(self, monkeypatch):
        # The one env the deploy sets, and the pair it derives (dfe-engine#332).
        monkeypatch.setenv("DFE_KAFKA_PROVIDER", "strimzi-no-tls")
        ks = KafkaSettings(**_get_env_overrides()["kafka"])
        assert (ks.security_protocol, ks.sasl_mechanism) == ("SASL_PLAINTEXT", "SCRAM-SHA-512")


class TestTopicEnvWiring:
    """The topic-creation dials bind to env. Unset means "follow the transport",
    so a brokerless profile needs no second dial; setting the env is the operator
    overriding that fact from the deploy that configures it."""

    @pytest.fixture(autouse=True)
    def _hermetic(self, monkeypatch):
        for key in list(os.environ):
            if key.startswith(("DFE_", "KAFKA_")):
                monkeypatch.delenv(key, raising=False)

    def test_defaults_when_unset(self):
        # No topic env set, so the section is pruned entirely and the model defaults stand.
        assert "kafka" not in _get_env_overrides()
        ks = KafkaSettings()
        # None, not True: no override, so the deploy hook follows bus_present.
        assert ks.ensure_topics is None
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

    def test_retention_and_cleanup_bind(self, monkeypatch):
        monkeypatch.setenv("DFE_KAFKA_TOPIC_RETENTION_MS", "604800000")
        monkeypatch.setenv("DFE_KAFKA_TOPIC_CLEANUP_POLICY", "delete")
        ks = KafkaSettings(**_get_env_overrides()["kafka"])
        assert (ks.topic_retention_ms, ks.topic_cleanup_policy) == (604800000, "delete")
