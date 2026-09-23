"""The source topic hooks: they report, and they never fail the deploy or the delete.

``_ensure_source_topics`` is the wiring that makes a deploy create its own
``_land``/``_load`` topics, and ``_remove_source_topics`` is the wiring that takes
the same pair away when the source is deleted. The module's own behaviour is
covered in ``tests/unit/test_kafka/test_topics.py``; what is proven here is the
contract the two endpoints depend on - the off switch is honoured, a broker that
will not answer is reported rather than raised, and the result lands on the
response.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.api.v1.sources import _ensure_source_topics, _remove_source_topics
from dfe_engine.kafka.topics import TopicEnsureResult, TopicRemoveResult
from dfe_engine.settings import KafkaSettings, TransportSettings
from dfe_engine.source.models import Source, SourceMatch, SourceTransform


def _settings(*, bus_present: bool = True, **kafka) -> SimpleNamespace:
    return SimpleNamespace(
        kafka=KafkaSettings(**kafka),
        transport=TransportSettings(
            bus_present=bus_present, default="bus" if bus_present else "direct"
        ),
    )


def _source(name="filebeat", *, transform=True) -> Source:
    return Source(
        source=name,
        match=SourceMatch(field="f", value="v"),
        transform=SourceTransform(engine="vector") if transform else None,
    )


class TestOffSwitch:
    def test_disabled_reaches_no_broker_at_all(self, monkeypatch):
        """The operator's override; the hook must not try to find a broker."""

        def _boom(*args, **kwargs):
            raise AssertionError("ensure_topics called while it was switched off")

        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _boom)
        assert _ensure_source_topics(_source(), _settings(ensure_topics=False)) == ([], [], [])

    def test_a_brokerless_deployment_reaches_no_broker_either(self, monkeypatch):
        """No bus is the fact the unset switch follows, so one dial does it."""

        def _boom(*args, **kwargs):
            raise AssertionError("ensure_topics called on a deployment with no bus")

        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _boom)
        assert _ensure_source_topics(_source(), _settings(bus_present=False)) == ([], [], [])

    def test_an_explicit_yes_overrides_the_brokerless_fact(self, monkeypatch):
        """An operator pointing at a broker the profile does not know about."""
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.ensure_topics",
            lambda specs, **kw: TopicEnsureResult(created=[s.name for s in specs]),
        )
        ensured, failed, _ = _ensure_source_topics(
            _source(), _settings(bus_present=False, ensure_topics=True)
        )

        assert ensured == ["filebeat_land", "filebeat_load"]
        assert failed == []


class TestReporting:
    def test_created_and_existing_are_both_reported_as_ensured(self, monkeypatch):
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.ensure_topics",
            lambda specs, **kw: TopicEnsureResult(
                created=["filebeat_load"], existing=["filebeat_land"]
            ),
        )
        ensured, failed, _ = _ensure_source_topics(_source(), _settings())
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
        ensured, failed, _ = _ensure_source_topics(_source(), _settings())
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


class TestTheDeleteHook:
    """One dial governs both ends, so a topic the engine made it also takes away."""

    def _capture(self, monkeypatch) -> dict:
        seen: dict = {}

        def _remove(names, **kw):
            seen["names"] = list(names)
            return TopicRemoveResult(removed=list(names))

        monkeypatch.setattr("dfe_engine.kafka.topics.remove_topics", _remove)
        return seen

    def test_it_removes_the_pair_the_deploy_created(self, monkeypatch):
        seen = self._capture(monkeypatch)

        removed, failed = _remove_source_topics(_source(), _settings())

        assert seen["names"] == ["filebeat_land", "filebeat_load"]
        assert (removed, failed) == (["filebeat_land", "filebeat_load"], [])

    def test_a_source_that_never_transformed_gives_up_only_its_landing_topic(self, monkeypatch):
        seen = self._capture(monkeypatch)

        _remove_source_topics(_source("syslog", transform=False), _settings())

        assert seen["names"] == ["syslog_land"]

    def test_the_off_switch_reaches_no_broker_at_all(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("remove_topics called while it was switched off")

        monkeypatch.setattr("dfe_engine.kafka.topics.remove_topics", _boom)

        assert _remove_source_topics(_source(), _settings(ensure_topics=False)) == ([], [])

    def test_a_brokerless_deployment_reaches_no_broker_either(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("remove_topics called on a deployment with no bus")

        monkeypatch.setattr("dfe_engine.kafka.topics.remove_topics", _boom)

        assert _remove_source_topics(_source(), _settings(bus_present=False)) == ([], [])

    def test_a_failure_is_reported_not_raised(self, monkeypatch):
        # The source is already gone by the time this runs, so a dead broker must
        # not turn a completed delete into a 500.
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.remove_topics",
            lambda names, **kw: TopicRemoveResult(
                failed=[("filebeat_land", "broker unreachable: connection refused")]
            ),
        )

        removed, failed = _remove_source_topics(_source(), _settings())

        assert (removed, failed) == ([], ["filebeat_land"])

    def test_a_topic_the_broker_never_had_is_not_reported_as_removed(self, monkeypatch):
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.remove_topics",
            lambda names, **kw: TopicRemoveResult(absent=list(names)),
        )

        assert _remove_source_topics(_source(), _settings()) == ([], [])
