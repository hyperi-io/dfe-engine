"""An existing topic whose shape differs from the manifest is reported, not adopted.

``ensure_topics`` leaves an existing topic untouched, so before dfe-engine#439 a
topic created by anything else kept its own shape and the pass still said
converged with nothing in ``refused``.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.kafka.topics import TopicEnsureResult, TopicUpdateResult
from dfe_engine.schema import phase
from dfe_engine.settings import DFESettings

SETTINGS = DFESettings(env="dev")


class _Rendered:
    def __init__(self, name: str) -> None:
        self.topic = {
            "name": name,
            "partitions": 3,
            "replication_factor": 3,
            "config": {"retention.ms": "604800000"},
        }


@pytest.fixture
def plan():
    return SimpleNamespace(topics=lambda: [_Rendered("main_land")])


@pytest.fixture(autouse=True)
def _no_skip(monkeypatch):
    monkeypatch.setattr(phase, "_topics_skipped", lambda settings: "")


def test_a_refused_shape_difference_is_reported(monkeypatch, plan):
    seen: dict[str, object] = {}

    def _compare(specs, **kw):
        seen.update(kw)
        return TopicUpdateResult(refused=[("main_land", "replication factor is 1, not 3")])

    monkeypatch.setattr(
        "dfe_engine.kafka.topics.ensure_topics",
        lambda specs, **kw: TopicEnsureResult(existing=["main_land"]),
    )
    monkeypatch.setattr("dfe_engine.kafka.topics.update_topics", _compare)

    created, skipped, drift = phase._apply_topics(plan, settings=SETTINGS)

    assert created == []
    assert skipped == ""
    assert drift == ["main_land: replication factor is 1, not 3"]
    # Without this the comparison could start APPLYING and every case still pass.
    assert seen["dry_run"] is True


def test_config_and_partition_differences_are_named(monkeypatch, plan):
    monkeypatch.setattr(
        "dfe_engine.kafka.topics.ensure_topics",
        lambda specs, **kw: TopicEnsureResult(existing=["main_land"]),
    )
    monkeypatch.setattr(
        "dfe_engine.kafka.topics.update_topics",
        lambda specs, **kw: TopicUpdateResult(altered=["main_land"], widened=["main_land"]),
    )

    _, _, drift = phase._apply_topics(plan, settings=SETTINGS)

    assert "main_land: config differs from the manifest" in drift
    assert "main_land: fewer partitions than the manifest asks for" in drift


def test_a_topic_that_matches_reports_nothing(monkeypatch, plan):
    monkeypatch.setattr(
        "dfe_engine.kafka.topics.ensure_topics",
        lambda specs, **kw: TopicEnsureResult(existing=["main_land"]),
    )
    monkeypatch.setattr(
        "dfe_engine.kafka.topics.update_topics",
        lambda specs, **kw: TopicUpdateResult(unchanged=["main_land"]),
    )

    assert phase._apply_topics(plan, settings=SETTINGS)[2] == []


def test_a_freshly_created_topic_is_not_compared(monkeypatch, plan):
    """Nothing existed, so there is no other shape to differ from."""
    monkeypatch.setattr(
        "dfe_engine.kafka.topics.ensure_topics",
        lambda specs, **kw: TopicEnsureResult(created=["main_land"]),
    )

    def _fail(*a, **kw):
        raise AssertionError("update_topics must not run when nothing pre-existed")

    monkeypatch.setattr("dfe_engine.kafka.topics.update_topics", _fail)

    created, _, drift = phase._apply_topics(plan, settings=SETTINGS)

    assert created == ["main_land"]
    assert drift == []


def test_an_undescribable_topic_reports_unknown_not_clean(monkeypatch, plan):
    """``update_topics`` does NOT raise on a describe fault -- it collects the
    topic into ``failed`` and returns. Reading only refused/altered/widened
    would report a converged deployment with no drift, which is the condition
    #439 exists to surface.
    """
    monkeypatch.setattr(
        "dfe_engine.kafka.topics.ensure_topics",
        lambda specs, **kw: TopicEnsureResult(existing=["main_land"]),
    )
    monkeypatch.setattr(
        "dfe_engine.kafka.topics.update_topics",
        lambda specs, **kw: TopicUpdateResult(
            failed=[("main_land", "KafkaError: DescribeConfigs not authorized")]
        ),
    )

    _, _, drift = phase._apply_topics(plan, settings=SETTINGS)

    assert drift == [
        "main_land: shape unreadable, so drift is unknown -- "
        "KafkaError: DescribeConfigs not authorized"
    ]


def test_a_raising_compare_does_not_fail_the_create_pass(monkeypatch, plan):
    """Defensive: the compare is not allowed to lose a successful create."""
    monkeypatch.setattr(
        "dfe_engine.kafka.topics.ensure_topics",
        lambda specs, **kw: TopicEnsureResult(created=["a_land"], existing=["main_land"]),
    )

    def _boom(*a, **kw):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr("dfe_engine.kafka.topics.update_topics", _boom)

    created, skipped, drift = phase._apply_topics(plan, settings=SETTINGS)

    assert created == ["a_land"]
    assert skipped == ""
    assert drift == []
