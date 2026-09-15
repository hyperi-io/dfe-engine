"""The governed topic routes (dfe-engine#97).

The admin behaviour itself is covered in ``tests/unit/test_kafka/test_topics.py``;
what is proven here is the API contract - the topic set is derived from the named
source rather than typed by the caller, a destructive remove is guarded, RBAC is
on every route, and a deployment that manages no topics says so instead of
answering with an empty list.

No broker is reached: the four service functions are patched on the router module
with fakes that record what the route asked for.
"""

from __future__ import annotations

import pytest

import dfe_engine.api.v1.kafka_topics as kafka_topics_module
from dfe_engine.kafka.topics import (
    TopicEnsureResult,
    TopicRemoveResult,
    TopicState,
    TopicStatusResult,
    TopicUpdateResult,
)

BASE = "/api/v1/kafka/topics"


@pytest.fixture
def source(client, admin_headers, sample_source) -> str:
    """A transforming source, so both its topics are in play."""
    body = {**sample_source, "source": "filebeat", "transform": {"engine": "vector"}}
    response = client.post("/api/v1/sources", json=body, headers=admin_headers)
    assert response.status_code in (200, 201), response.text
    return "filebeat"


@pytest.fixture
def recorded(monkeypatch) -> dict:
    """Capture what each route hands the topic layer, and answer without a broker."""
    seen: dict = {}

    def _status(specs, *, sources=None, **kw):
        seen["status"] = [s.name for s in specs]
        seen["owners"] = dict(sources or {})
        return TopicStatusResult(
            topics=[
                TopicState(
                    name=spec.name,
                    source=(sources or {}).get(spec.name, ""),
                    exists=True,
                    desired_partitions=spec.partitions,
                    desired_replication_factor=spec.replication_factor,
                    partitions=spec.partitions,
                    replication_factor=spec.replication_factor,
                )
                for spec in specs
            ]
        )

    def _ensure(specs, **kw):
        seen["ensure"] = [s.name for s in specs]
        seen["ensure_dry_run"] = kw.get("dry_run")
        return TopicEnsureResult(created=[s.name for s in specs])

    def _update(specs, **kw):
        seen["update"] = [s.name for s in specs]
        seen["update_dry_run"] = kw.get("dry_run")
        return TopicUpdateResult(
            widened=[s.name for s in specs], refused=[("filebeat_land", "no shrink")]
        )

    def _remove(names, **kw):
        seen["remove"] = list(names)
        seen["remove_dry_run"] = kw.get("dry_run")
        return TopicRemoveResult(removed=list(names))

    monkeypatch.setattr(kafka_topics_module, "topic_status", _status)
    monkeypatch.setattr(kafka_topics_module, "ensure_topics", _ensure)
    monkeypatch.setattr(kafka_topics_module, "update_topics", _update)
    monkeypatch.setattr(kafka_topics_module, "remove_topics", _remove)
    return seen


class TestStatus:
    def test_it_reports_the_topics_a_source_implies(self, client, admin_headers, source, recorded):
        response = client.get(f"{BASE}?source={source}", headers=admin_headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reachable"] is True
        assert [t["name"] for t in body["topics"]] == ["filebeat_land", "filebeat_load"]
        assert {t["source"] for t in body["topics"]} == {"filebeat"}

    def test_omitting_the_source_covers_every_source(self, client, admin_headers, source, recorded):
        response = client.get(BASE, headers=admin_headers)

        assert response.status_code == 200, response.text
        assert "filebeat_land" in recorded["status"]

    def test_an_unknown_source_is_a_404(self, client, admin_headers, recorded):
        response = client.get(f"{BASE}?source=nope", headers=admin_headers)

        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_an_unreachable_broker_is_reported_not_raised(
        self, client, admin_headers, source, monkeypatch
    ):
        monkeypatch.setattr(
            kafka_topics_module,
            "topic_status",
            lambda specs, **kw: TopicStatusResult(error="broker unreachable: refused"),
        )

        response = client.get(BASE, headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["reachable"] is False
        assert "refused" in response.json()["error"]


class TestEnsure:
    def test_it_creates_the_source_topics(self, client, admin_headers, source, recorded):
        response = client.post(f"{BASE}/ensure?source={source}", headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["created"] == ["filebeat_land", "filebeat_load"]
        assert recorded["ensure_dry_run"] is False

    def test_dry_run_is_passed_through(self, client, admin_headers, source, recorded):
        response = client.post(f"{BASE}/ensure?source={source}&dry_run=true", headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["dry_run"] is True
        assert recorded["ensure_dry_run"] is True


class TestUpdate:
    def test_refusals_are_reported_apart_from_failures(
        self, client, admin_headers, source, recorded
    ):
        """An operator fixes a refusal and a failure in different places."""
        response = client.post(f"{BASE}/update?source={source}", headers=admin_headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["widened"] == ["filebeat_land", "filebeat_load"]
        assert body["refused"] == [{"name": "filebeat_land", "error": "no shrink"}]
        assert body["failed"] == []


class TestRemove:
    def test_it_refuses_without_the_confirmation(self, client, admin_headers, source, recorded):
        response = client.post(f"{BASE}/remove?source={source}", headers=admin_headers)

        assert response.status_code == 400
        assert response.json()["code"] == "confirmation_required"
        assert "remove" not in recorded

    def test_a_dry_run_needs_no_confirmation(self, client, admin_headers, source, recorded):
        response = client.post(f"{BASE}/remove?source={source}&dry_run=true", headers=admin_headers)

        assert response.status_code == 200, response.text
        assert recorded["remove_dry_run"] is True

    def test_confirmed_it_removes_the_pair_the_deploys_created(
        self, client, admin_headers, source, recorded
    ):
        response = client.post(f"{BASE}/remove?source={source}&confirm=true", headers=admin_headers)

        assert response.status_code == 200, response.text
        assert response.json()["removed"] == ["filebeat_land", "filebeat_load"]

    def test_there_is_no_remove_everything_call(self, client, admin_headers, recorded):
        """The source is required, so no caller can empty the broker by omission."""
        response = client.post(f"{BASE}/remove?confirm=true", headers=admin_headers)

        assert response.status_code == 422
        assert "remove" not in recorded


class TestRbac:
    def test_a_viewer_may_read_the_status(self, client, viewer_headers, source, recorded):
        assert client.get(BASE, headers=viewer_headers).status_code == 200

    @pytest.mark.parametrize("route", ["ensure", "update"])
    def test_a_viewer_may_not_change_anything(
        self, client, viewer_headers, source, recorded, route
    ):
        response = client.post(f"{BASE}/{route}?source=filebeat", headers=viewer_headers)

        assert response.status_code == 403
        assert route not in recorded

    def test_a_viewer_may_not_remove(self, client, viewer_headers, source, recorded):
        response = client.post(
            f"{BASE}/remove?source=filebeat&confirm=true", headers=viewer_headers
        )

        assert response.status_code == 403
        assert "remove" not in recorded

    def test_an_anonymous_caller_gets_nothing(self, client, source):
        assert client.get(BASE).status_code == 401


class TestUnmanagedDeployment:
    """A brokerless profile says so rather than answering with an empty list."""

    @pytest.fixture(autouse=True)
    def _unmanaged(self, monkeypatch):
        monkeypatch.setattr(kafka_topics_module, "topics_managed", lambda settings: False)

    def test_status_is_503(self, client, admin_headers):
        response = client.get(BASE, headers=admin_headers)

        assert response.status_code == 503
        assert response.json()["code"] == "not_configured"

    def test_ensure_is_503(self, client, admin_headers):
        assert client.post(f"{BASE}/ensure", headers=admin_headers).status_code == 503
