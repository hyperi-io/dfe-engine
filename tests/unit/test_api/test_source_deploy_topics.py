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
    sweep_stranded_topics,
)
from dfe_engine.appmgmt.instances import instance_of
from dfe_engine.kafka.topics import TopicEnsureResult, TopicRemoveResult
from dfe_engine.settings import KafkaSettings, TransportSettings
from dfe_engine.source.models import (
    Source,
    SourceFetcher,
    SourceMatch,
    SourceTransform,
    _topic_policy,
)
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
        assert _ensure_source_topics(_source(), _settings(ensure_topics=False)) == ([], [], [], [])

    def test_a_brokerless_deployment_reaches_no_broker_either(self, monkeypatch):
        """No bus is the fact the unset switch follows, so one dial does it."""

        def _boom(*args, **kwargs):
            raise AssertionError("ensure_topics called on a deployment with no bus")

        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _boom)
        assert _ensure_source_topics(_source(), _settings(bus_present=False)) == ([], [], [], [])

    def test_an_explicit_yes_overrides_the_brokerless_fact(self, monkeypatch):
        """An operator pointing at a broker the profile does not know about."""
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.ensure_topics",
            lambda specs, **kw: TopicEnsureResult(created=[s.name for s in specs]),
        )
        ensured, failed, *_ = _ensure_source_topics(
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
        ensured, failed, *_ = _ensure_source_topics(_source(), _settings())
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

        ensured, failed, *_ = _ensure_source_topics(
            _source(), _settings(bus_present=False, ensure_topics=True)
        )

        assert (ensured, failed) == (["filebeat_land"], ["filebeat_load"])

    def test_a_bus_deployment_whose_stages_run_direct_reports_it(self, monkeypatch):
        monkeypatch.setattr("dfe_engine.kafka.topics.ensure_topics", _half_refused)

        ensured, failed, *_ = _ensure_source_topics(_source(), _settings(default="direct"))

        assert (ensured, failed) == (["filebeat_land"], ["filebeat_load"])


class _StrandedBus:
    """A one-broker bus still holding the ``_load`` topic a dropped transform wrote."""

    def __init__(
        self, load: str, *, unread: int, land: str = "", writer: tuple[str, int] = ("empty", 0)
    ) -> None:
        self.present = {land or load.replace("_load", "_land"), load}
        self.load = load
        self.unread = unread
        self.writer = writer
        self.described: list[str] = []
        self.deleted: list[str] = []

    def node_count(self, **_kwargs) -> int:
        return 1

    def list_topic_names(self, **_kwargs) -> set[str]:
        return self.present - set(self.deleted)

    def describe_group(self, group: str) -> tuple[str, int]:
        self.described.append(group)
        return self.writer

    def watermarks(self, name: str) -> dict[int, tuple[int, int]]:
        return {0: (0, 10)}

    def committed_offsets(self, name: str, partitions: list[int]) -> dict[str, dict[int, int]]:
        return {"dfe-loader": {0: 10 - self.unread}}

    def delete(self, name: str) -> None:
        self.deleted.append(name)


class _Sources:
    """The registry's view of these sources."""

    def __init__(self, *sources: Source) -> None:
        self.by_name = {source.source: source for source in sources}

    def get_all_sources(self, states=None) -> list[Source]:
        return list(self.by_name.values())

    def source_exists(self, name: str) -> bool:
        return name in self.by_name

    def get_source(self, name: str) -> Source:
        return self.by_name[name]


class _DeployRepo:
    """A deploy repo carrying a transform instance for each source named."""

    def __init__(self, *running: str) -> None:
        self.names: list[str] = [
            instance_of("dfe-transform-vector", name).overlay_name for name in running
        ]

    def list(self, _resource_class: str):
        return self.names


def _main_lander() -> Source:
    return Source(  # ty: ignore[pydantic-discarded-extra-argument]  # Source lifts its flat fields into the version tree in a before-validator
        source="aws-cloudtrail", fetcher=SourceFetcher(source_type="aws", topic="main")
    )


def _dropped(name: str = "syslog") -> Source:
    """A source deployed at a version without the transform its first version had."""
    source = _source(name)
    source.versions["2.0.0"] = source.versions[source.current].model_copy(
        update={"transform": None}
    )
    source.current = "2.0.0"
    source.deployed_version = "2.0.0"
    return source


class TestAStrandedLoadTopic:
    """The loader skips ``_land`` while ``_load`` exists, so a dropped transform's topic must go."""

    def _deploy(
        self, monkeypatch, bus: _StrandedBus, *, gc, source=None, settings=None, registry=None
    ):
        monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", lambda **_kw: bus)
        return _ensure_source_topics(
            source or _dropped(),
            settings or _settings(),
            registry=registry,
            gc=gc,
        )

    def test_a_drained_topic_no_transform_instance_writes_is_deleted(self, monkeypatch):
        bus = _StrandedBus("syslog_load", unread=0)

        topics = self._deploy(monkeypatch, bus, gc=_DeployRepo())

        assert (topics.removed, topics.pending) == (["syslog_load"], [])
        assert bus.deleted == ["syslog_load"]
        assert bus.described == ["dfe-transform-vector-syslog"]

    def test_a_live_member_in_the_transforms_group_keeps_a_drained_topic(self, monkeypatch):
        # Gone from the deploy repo, but its pod still runs until Argo syncs and it drains.
        bus = _StrandedBus("syslog_load", unread=0, writer=("stable", 1))

        topics = self._deploy(monkeypatch, bus, gc=_DeployRepo())

        assert (topics.removed, topics.pending, bus.deleted) == ([], ["syslog_load"], [])

    def test_a_topic_whose_writer_has_no_group_to_name_is_kept(self, monkeypatch):
        # No version names a transform, so nothing says which group wrote the topic.
        bus = _StrandedBus("syslog_load", unread=0)

        topics = self._deploy(
            monkeypatch, bus, gc=_DeployRepo(), source=_source("syslog", transform=False)
        )

        assert (topics.removed, topics.pending, bus.described) == ([], ["syslog_load"], [])

    def test_a_topic_with_unread_records_is_pending_and_the_deploy_goes_ahead(self, monkeypatch):
        bus = _StrandedBus("syslog_load", unread=3)

        topics = self._deploy(monkeypatch, bus, gc=_DeployRepo())

        assert (topics.ensured, topics.removed, topics.pending) == (
            ["syslog_land"],
            [],
            ["syslog_load"],
        )
        assert bus.deleted == []

    def test_a_drained_topic_its_transform_instance_still_writes_is_pending(self, monkeypatch):
        # The instance leaves the deploy repo after this step, and its pod later still.
        bus = _StrandedBus("syslog_load", unread=0)

        topics = self._deploy(monkeypatch, bus, gc=_DeployRepo("syslog"))

        assert (topics.removed, topics.pending, bus.deleted) == ([], ["syslog_load"], [])

    def test_without_a_deploy_repo_a_drained_topic_is_pending(self, monkeypatch):
        bus = _StrandedBus("syslog_load", unread=0)

        topics = self._deploy(monkeypatch, bus, gc=None)

        assert (topics.removed, topics.pending, bus.deleted) == ([], ["syslog_load"], [])

    def test_off_the_bus_nothing_reads_it_so_it_is_left_alone(self, monkeypatch):
        bus = _StrandedBus("syslog_load", unread=3)

        topics = self._deploy(
            monkeypatch, bus, gc=_DeployRepo(), settings=_settings(default="direct")
        )

        assert (topics.removed, topics.pending, bus.deleted) == ([], [], [])

    def test_a_topic_another_sources_live_transform_writes_is_kept(self, monkeypatch):
        # main_load is main's own output while main is deployed with a transform.
        owner = _source("main")
        owner.deployed_version = owner.current
        bus = _StrandedBus("main_load", unread=0)

        topics = self._deploy(
            monkeypatch, bus, gc=_DeployRepo(), source=_main_lander(), registry=_Sources(owner)
        )

        assert (topics.removed, topics.pending, bus.deleted) == ([], [], [])

    def test_a_topic_whose_owner_no_longer_transforms_is_deleted(self, monkeypatch):
        bus = _StrandedBus("main_load", unread=0)

        topics = self._deploy(
            monkeypatch,
            bus,
            gc=_DeployRepo(),
            source=_main_lander(),
            registry=_Sources(_dropped("main")),
        )

        assert topics.removed == ["main_load"]
        assert bus.described == ["dfe-transform-vector-main"]


class TestTheLaterPass:
    """The apps reconcile deletes a pending topic once the instance left before it ran."""

    def _sweep(self, monkeypatch, bus, registry, transforms: set[str]):
        monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", lambda **_kw: bus)
        return sweep_stranded_topics(registry, _settings(), transforms, actor="admin")

    def test_a_drained_topic_whose_instance_had_gone_is_deleted_and_audited(
        self, monkeypatch, audit_events
    ):
        bus = _StrandedBus("syslog_load", unread=0)

        swept = self._sweep(monkeypatch, bus, _Sources(_dropped()), transforms=set())

        assert swept == (["syslog_load"], [])
        assert bus.deleted == ["syslog_load"]
        (audit,) = [e for e in audit_events if e.get("event") == "resource.schema.topics_removed"]
        assert (audit["resource_name"], audit["details"]) == (
            "syslog",
            {"topics_removed": ["syslog_load"]},
        )

    def test_the_pass_that_removes_the_instance_keeps_the_topic(self, monkeypatch):
        bus = _StrandedBus("syslog_load", unread=0)

        swept = self._sweep(monkeypatch, bus, _Sources(_dropped()), transforms={"syslog"})

        assert (swept, bus.deleted) == (([], ["syslog_load"]), [])

    def test_a_topic_with_unread_records_is_never_deleted(self, monkeypatch):
        bus = _StrandedBus("syslog_load", unread=2)

        swept = self._sweep(monkeypatch, bus, _Sources(_dropped()), transforms=set())

        assert (swept, bus.deleted) == (([], ["syslog_load"]), [])

    def test_a_deployment_that_never_dropped_a_transform_asks_no_broker(self, monkeypatch):
        def _boom(**_kwargs):
            raise AssertionError("the broker was asked with no _load topic ever made")

        monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", _boom)
        registry = _Sources(_source("syslog", transform=False))

        assert sweep_stranded_topics(registry, _settings(), set(), actor="admin") == ([], [])

    def test_an_unreachable_broker_never_fails_the_reconcile(self, monkeypatch):
        def _unreachable(**_kwargs):
            raise RuntimeError("no broker")

        monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", _unreachable)

        assert sweep_stranded_topics(_Sources(_dropped()), _settings(), set(), actor="admin") == (
            [],
            [],
        )


class TestTheDeployRoute:
    """A topic not created answers 502 undeployed, and a stranded one never stops the deploy."""

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

    def _stranded_deploy(
        self, client, admin_headers, sample_source, monkeypatch, *, unread: int, transforms
    ):
        """Deploy *sample_source* with a transform, then without it over a bus still holding its ``_load`` topic."""
        name = sample_source["source"]
        vector = {"engine": "vector", "config_file": f"/etc/vector/{name}.yaml"}
        created = client.post(
            "/api/v1/sources", json={**sample_source, "transform": vector}, headers=admin_headers
        )
        assert created.status_code == 201, created.text
        self._fake_ddl(monkeypatch)
        bus = _StrandedBus(f"{name}_load", unread=unread)
        monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", lambda **_kw: bus)
        monkeypatch.setattr(sources_api, "transform_instances", lambda _gc: transforms)
        first = client.post(f"/api/v1/sources/{name}/deploy", headers=admin_headers)
        assert first.status_code == 200, first.text
        dropped = client.put(
            f"/api/v1/sources/{name}",
            json={**sample_source, "transform": None},
            headers=admin_headers,
        )
        assert dropped.status_code == 200, dropped.text
        resp = client.post(f"/api/v1/sources/{name}/deploy", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        stored = client.get(f"/api/v1/sources/{name}", headers=admin_headers).json()
        assert stored["deployed_version"] == stored["current"]
        return name, resp.json(), bus

    def test_a_stranded_topic_with_records_is_pending_and_the_source_deploys(
        self, client, admin_headers, sample_source, monkeypatch, audit_events
    ):
        name, body, bus = self._stranded_deploy(
            client, admin_headers, sample_source, monkeypatch, unread=4, transforms=set()
        )

        assert (body["topics_removed"], body["topics_pending_removal"]) == ([], [f"{name}_load"])
        assert bus.deleted == []
        *_, audit = [e for e in audit_events if e.get("event") == "resource.schema.deployed"]
        assert audit["details"]["topics_pending_removal"] == [f"{name}_load"]

    def test_a_drained_stranded_topic_is_deleted_and_the_deploy_goes_ahead(
        self, client, admin_headers, sample_source, monkeypatch
    ):
        name, body, bus = self._stranded_deploy(
            client, admin_headers, sample_source, monkeypatch, unread=0, transforms=set()
        )

        assert (body["topics_removed"], body["topics_pending_removal"]) == ([f"{name}_load"], [])
        assert bus.deleted == [f"{name}_load"]

    def test_a_drained_topic_its_transform_instance_writes_is_pending(
        self, client, admin_headers, sample_source, monkeypatch
    ):
        name = sample_source["source"]
        _, body, bus = self._stranded_deploy(
            client, admin_headers, sample_source, monkeypatch, unread=0, transforms={name}
        )

        assert (body["topics_removed"], body["topics_pending_removal"]) == ([], [f"{name}_load"])
        assert bus.deleted == []


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
