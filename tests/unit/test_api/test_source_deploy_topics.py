"""The source topic hooks: a bus source's deploy fails on a missing topic, and the rest report.

``_ensure_source_topics`` is the wiring that makes a deploy create its own
``_land``/``_load`` topics, and ``_remove_source_topics`` is the wiring that takes
the same pair away when the source is deleted. The module's own behaviour is
covered in ``tests/unit/test_kafka/test_topics.py``; what is proven here is the
contract the two endpoints depend on - the off switch is honoured, a topic the
broker did not create fails the deploy of a source on the bus and is reported
everywhere else, and the result lands on the response.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.api.v1 import sources as sources_api
from dfe_engine.api.v1.sources import (
    _ensure_source_topics,
    _remove_source_topics,
    _TopicsNotCreatedError,
)
from dfe_engine.kafka.topics import TopicEnsureResult, TopicRemoveResult
from dfe_engine.settings import KafkaSettings, TransportSettings
from dfe_engine.source.models import Source, SourceMatch, SourceTransform, _topic_policy
from dfe_engine.transport import SourceTransport

REFUSED = "the broker refused it (POLICY_VIOLATION); the engine log has the reason"


def _settings(
    *, bus_present: bool = True, default: SourceTransport | None = None, **kafka
) -> SimpleNamespace:
    transport: SourceTransport = default or ("bus" if bus_present else "direct")
    return SimpleNamespace(
        kafka=KafkaSettings(**kafka),
        transport=TransportSettings(bus_present=bus_present, default=transport),
    )


def _source(name="filebeat", *, transform=True) -> Source:
    return Source(  # ty: ignore[pydantic-discarded-extra-argument]  # Source lifts its flat fields into the version tree in a before-validator
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

    def test_the_configured_width_reaches_the_specs(self, monkeypatch):
        seen = {}

        def _capture(specs, **kw):
            seen["specs"] = [(s.name, s.partitions, s.replication_factor) for s in specs]
            return TopicEnsureResult(created=[s.name for s in specs])

        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _capture)
        _ensure_source_topics(_source(), _settings(topic_partitions=6, topic_replication_factor=3))
        assert seen["specs"] == [("filebeat_land", 6, 3), ("filebeat_load", 6, 3)]

    @pytest.mark.parametrize("configured", [8_388_608, None])
    def test_every_source_topic_carries_the_deployment_message_size(self, monkeypatch, configured):
        # A _land topic on the broker default refuses what the receiver accepts.
        seen = {}

        def _capture(specs, **kw):
            seen["sizes"] = {s.name: s.config.get("max.message.bytes") for s in specs}
            return TopicEnsureResult(created=[s.name for s in specs])

        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _capture)
        kafka = {} if configured is None else {"topic_max_message_bytes": configured}
        _ensure_source_topics(_source(), _settings(**kafka))
        size = configured or _topic_policy().defaults["max_message_bytes"]
        assert seen["sizes"] == {"filebeat_land": str(size), "filebeat_load": str(size)}

    def test_a_source_without_a_transform_asks_for_land_only(self, monkeypatch):
        seen = {}

        def _capture(specs, **kw):
            seen["names"] = [s.name for s in specs]
            return TopicEnsureResult(created=list(seen["names"]))

        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _capture)
        _ensure_source_topics(_source("syslog", transform=False), _settings())
        assert seen["names"] == ["syslog_land"]


class _Bus:
    """A bus of a given size that holds no topics yet."""

    def __init__(self, brokers: int) -> None:
        self.brokers = brokers

    def node_count(self, **_kwargs) -> int:
        return self.brokers

    def list_topic_names(self, **_kwargs) -> set[str]:
        return set()


class TestReplicationFactor:
    """A managed broker refuses a topic below 3 replicas, so unset must not mean 1."""

    def _deploy(self, monkeypatch, settings, *, brokers: int) -> dict:
        seen: dict = {"admins": 0}

        def _build_admin(**_kwargs):
            seen["admins"] += 1
            return _Bus(brokers)

        def _ensure(specs, **kw):
            seen["factors"] = {s.name: s.replication_factor for s in specs}
            return TopicEnsureResult(created=[s.name for s in specs])

        monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", _build_admin)
        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _ensure)
        _ensure_source_topics(_source(), settings)
        return seen

    def test_unset_creates_at_three_on_a_managed_broker(self, monkeypatch):
        seen = self._deploy(monkeypatch, _settings(), brokers=6)

        assert seen["factors"] == {"filebeat_land": 3, "filebeat_load": 3}

    def test_unset_on_one_broker_asks_for_one(self, monkeypatch):
        seen = self._deploy(monkeypatch, _settings(), brokers=1)

        assert seen["factors"] == {"filebeat_land": 1, "filebeat_load": 1}

    def test_a_configured_factor_is_honoured_as_given(self, monkeypatch):
        seen = self._deploy(monkeypatch, _settings(topic_replication_factor=2), brokers=6)

        assert seen["factors"] == {"filebeat_land": 2, "filebeat_load": 2}

    def test_one_admin_reads_the_size_and_runs_the_deploy(self, monkeypatch):
        # A second admin is a second full timeout when the broker does not answer.
        seen = self._deploy(monkeypatch, _settings(), brokers=6)

        assert seen["admins"] == 1


def _half_refused(specs, **_kwargs) -> TopicEnsureResult:
    """The broker takes ``_land`` and refuses ``_load``."""
    return TopicEnsureResult(created=["filebeat_land"], failed=[("filebeat_load", REFUSED)])


class TestABusSourceFailsItsDeploy:
    """Its receiver answers 503 for every record until the topic exists."""

    def test_a_refused_topic_raises_naming_it_and_the_reason(self, monkeypatch):
        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _half_refused)

        with pytest.raises(_TopicsNotCreatedError) as raised:
            _ensure_source_topics(_source(), _settings())

        assert raised.value.failed == [("filebeat_load", REFUSED)]
        assert raised.value.ensured == ["filebeat_land"]

    def test_an_unreachable_broker_raises_too(self, monkeypatch):
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.ensure_topics",
            lambda specs, **kw: TopicEnsureResult(
                failed=[
                    (s.name, "broker unreachable; the engine log has the reason") for s in specs
                ]
            ),
        )

        with pytest.raises(_TopicsNotCreatedError) as raised:
            _ensure_source_topics(_source(), _settings())

        assert [topic for topic, _ in raised.value.failed] == ["filebeat_land", "filebeat_load"]

    @pytest.mark.parametrize(
        "outcome",
        [
            TopicEnsureResult(created=["filebeat_land"], existing=["filebeat_load"]),
            TopicEnsureResult(),
        ],
        ids=["all-ensured", "nothing-asked"],
    )
    def test_every_topic_ensured_does_not_raise(self, monkeypatch, outcome):
        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", lambda specs, **kw: outcome)

        _ensure_source_topics(_source(), _settings())


class TestOffTheBusAFailureIsReported:
    """No stage reads the topics there, so the deploy behaves as it always has."""

    def test_a_brokerless_deployment_told_to_manage_topics_reports_it(self, monkeypatch):
        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _half_refused)

        ensured, failed, _ = _ensure_source_topics(
            _source(), _settings(bus_present=False, ensure_topics=True)
        )

        assert (ensured, failed) == (["filebeat_land"], ["filebeat_load"])

    def test_a_bus_deployment_whose_stages_run_direct_reports_it(self, monkeypatch):
        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _half_refused)

        ensured, failed, _ = _ensure_source_topics(_source(), _settings(default="direct"))

        assert (ensured, failed) == (["filebeat_land"], ["filebeat_load"])


class TestTheDeployRoute:
    """The route answers 502 naming the topic, and the source is not marked deployed."""

    def _fake_ddl(self, monkeypatch) -> None:
        """The ClickHouse step as a deploy that applied one statement."""

        def _applied(name, registry, _settings, _version, _dry_run):
            source = registry.get_source(name)
            result = SimpleNamespace(create_table_ddl="CREATE TABLE t (a String)", columns=[])
            return sources_api._AppliedSchema(source, source.current, result, {}, 1, "dfe", None)

        monkeypatch.setattr(sources_api, "_apply_source_schema", _applied)
        monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", lambda **_kw: _Bus(3))

    def test_a_refused_topic_answers_502_and_leaves_the_source_undeployed(
        self, client, admin_headers, sample_source, monkeypatch, audit_events
    ):
        name = sample_source["source"]
        created = client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        assert created.status_code == 201, created.text
        self._fake_ddl(monkeypatch)
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.ensure_topics",
            lambda specs, **kw: TopicEnsureResult(failed=[(f"{name}_land", REFUSED)]),
        )

        resp = client.post(f"/api/v1/sources/{name}/deploy", headers=admin_headers)

        assert resp.status_code == 502, resp.text
        body = resp.json()
        assert body["code"] == "topics_not_created"
        assert body["context"] == {
            "source": name,
            "topics": [{"topic": f"{name}_land", "reason": REFUSED}],
        }
        assert f"{name}_land (the broker refused it (POLICY_VIOLATION)" in body["message"]
        stored = client.get(f"/api/v1/sources/{name}", headers=admin_headers).json()
        assert stored["deployed_version"] is None
        (audit,) = [e for e in audit_events if e.get("event") == "resource.schema.deploy_failed"]
        assert audit["details"]["topics_failed"] == [f"{name}_land"]


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
