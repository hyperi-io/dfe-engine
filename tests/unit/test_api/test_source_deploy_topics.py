"""The source-deploy topic hook: it reports, and it never fails the deploy.

``_ensure_source_topics`` is the wiring that makes a deploy create its own
``_land``/``_load`` topics. The module's own behaviour is covered in
``tests/unit/test_kafka/test_topics.py``; what is proven here is the contract the
deploy endpoint depends on - the off switch is honoured, a broker that will not
answer is reported rather than raised, and the result lands on the response.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.api.v1.sources import _ensure_source_topics
from dfe_engine.kafka.topics import TopicEnsureResult
from dfe_engine.settings import KafkaSettings
from dfe_engine.source.models import Source, SourceMatch, SourceTransform


def _settings(**kafka) -> SimpleNamespace:
    return SimpleNamespace(kafka=KafkaSettings(**kafka))


def _source(name="filebeat", *, transform=True) -> Source:
    return Source(
        source=name,
        match=SourceMatch(field="f", value="v"),
        transform=SourceTransform(engine="vector") if transform else None,
    )


class TestOffSwitch:
    def test_disabled_reaches_no_broker_at_all(self, monkeypatch):
        """The Kafka-less profile has no broker; the hook must not try to find one."""

        def _boom(*args, **kwargs):
            raise AssertionError("ensure_topics called while ensure_topics=False")

        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _boom)
        assert _ensure_source_topics(_source(), _settings(ensure_topics=False)) == ([], [])


class TestReporting:
    def test_created_and_existing_are_both_reported_as_ensured(self, monkeypatch):
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.ensure_topics",
            lambda specs, **kw: TopicEnsureResult(
                created=["filebeat_load"], existing=["filebeat_land"]
            ),
        )
        ensured, failed = _ensure_source_topics(_source(), _settings())
        assert ensured == ["filebeat_load", "filebeat_land"]
        assert failed == []

    def test_a_failure_is_reported_not_raised(self, monkeypatch):
        """A dead broker must not take the deploy down - the schema is already live."""
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.ensure_topics",
            lambda specs, **kw: TopicEnsureResult(
                failed=[("filebeat_land", "broker unreachable: connection refused")]
            ),
        )
        ensured, failed = _ensure_source_topics(_source(), _settings())
        assert ensured == []
        assert failed == ["filebeat_land"]

    def test_the_configured_width_reaches_the_specs(self, monkeypatch):
        seen = {}

        def _capture(specs, **kw):
            seen["specs"] = [(s.name, s.partitions, s.replication_factor) for s in specs]
            return TopicEnsureResult(created=[s.name for s in specs])

        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _capture)
        _ensure_source_topics(_source(), _settings(topic_partitions=6, topic_replication_factor=3))
        assert seen["specs"] == [("filebeat_land", 6, 3), ("filebeat_load", 6, 3)]

    def test_a_source_without_a_transform_asks_for_land_only(self, monkeypatch):
        seen = {}

        def _capture(specs, **kw):
            seen["names"] = [s.name for s in specs]
            return TopicEnsureResult(created=list(seen["names"]))

        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _capture)
        _ensure_source_topics(_source("syslog", transform=False), _settings())
        assert seen["names"] == ["syslog_land"]


class TestDeployIsNeverFailedByTopics:
    @pytest.mark.parametrize(
        "outcome",
        [
            TopicEnsureResult(failed=[("a", "boom")]),
            TopicEnsureResult(created=["a"]),
            TopicEnsureResult(),
        ],
    )
    def test_no_outcome_raises(self, monkeypatch, outcome):
        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", lambda specs, **kw: outcome)
        _ensure_source_topics(_source(), _settings())
