#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_backing_services_managed.py
#  Purpose:      Managed backing services: the facts the API reports and the 409 guard
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A backing service DFE does not run reads as managed, and refuses inert sizing writes.

Three things are pinned. A deployment that states no provider gets today's body
with ``managed_by: dfe`` and nothing else moves, which is every deployment until
dfe-infra sends the facts. A managed one reports who runs it and refuses a write to
a value nothing in the deployment reads, with 409, before the count guard and the
policy, and whatever the caller holds. And the observed read is attempted only
where DFE does not run the service, and a failed one is null rather than a 500.

Everything runs against a real dulwich deploy repo. ClickHouse is the engine's own
bounded client aimed at a listener that drops every connection, so the tests see
what a down server gives and can count whether it was dialled at all.
"""

import pytest

from dfe_engine.api.v1 import backing_services
from dfe_engine.settings import BackingFactSettings, BackingSettings, backing_from_environment
from tests.support.down_clickhouse import down_clickhouse
from tests.unit.test_api.test_backing_services import STORAGE_LOCK, _wire_gitcrud, _writer

_BACKING = "/api/v1/backing-services"

KAFKA_READ_ONLY = [
    "mode",
    "replicas",
    "storage",
    "storageModel",
    "resources",
    "objectStore",
    "tieredObject",
    "sizing",
    "controllerPool",
]
CLICKHOUSE_READ_ONLY = [
    "mode",
    "replicas",
    "keeper.replicas",
    "storage",
    "storageModel",
    "resources",
    "objectStore",
    "tieredBlock",
]
# An external service leaves mode writable: the declared mode is the way back to DFE.
KAFKA_EXTERNAL = KAFKA_READ_ONLY[1:]
CLICKHOUSE_EXTERNAL = CLICKHOUSE_READ_ONLY[1:]

# The fields GET /backing-services carried before managed services existed.
DECLARED_FIELDS = {
    "service",
    "chart",
    "overlay",
    "prefix",
    "mode",
    "storage_model",
    "replicas",
    "storage_size",
    "storage_class",
    "resources",
}
ADDED_FIELDS = {
    "managed_by",
    "provider",
    "tier",
    "region",
    "read_only",
    "observed",
    "limits",
    "principals",
}
SELF_RUN_FIELDS = {
    "managed_by": "dfe",
    "provider": None,
    "tier": None,
    "region": None,
    "read_only": [],
    "observed": None,
    "limits": [],
    "principals": [],
}
UNKNOWN_SIZE = {
    "brokers": None,
    "replicas": None,
    "shards": None,
    "memory_bytes": None,
    "provider_replicas": None,
    "provider_min_replica_memory_gb": None,
    "provider_max_replica_memory_gb": None,
    "provider_idle_scaling": None,
}


@pytest.fixture
def down_ch(app, monkeypatch):
    """Route the observed ClickHouse read to a server that drops every connection."""
    with down_clickhouse(app.state.settings) as (listener, client):
        monkeypatch.setattr(
            backing_services, "get_interactive_clickhouse_client", lambda _s: client
        )
        yield listener


def _state(app, *, kafka: BackingFactSettings | None = None, ch: BackingFactSettings | None = None):
    """Have the deployer state these facts, as its environment would."""
    app.state.settings.deployment.backing = BackingSettings(
        kafka=kafka or BackingFactSettings(), clickhouse=ch or BackingFactSettings()
    )


CONFLUENT = BackingFactSettings(provider="confluent-cloud", tier="basic", region="us-east-1")
MSK = BackingFactSettings(provider="msk", tier="express.m7g.large")
CH_CLOUD = BackingFactSettings(provider="clickhouse-cloud")
CH_EXTERNAL = BackingFactSettings(provider="external")


def _by_service(client, headers) -> dict[str, dict]:
    resp = client.get(_BACKING, headers=headers)
    assert resp.status_code == 200, resp.text
    return {s["service"]: s for s in resp.json()}


class TestNoStatedProvider:
    """Every deployment until the facts arrive: today's body, plus managed_by dfe."""

    def test_the_body_is_todays_plus_the_self_run_fields(
        self, client, app, admin_headers, tmp_path, down_ch
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 3}}, "tester")

        body = _by_service(client, admin_headers)

        for service in body.values():
            assert set(service) == DECLARED_FIELDS | ADDED_FIELDS
            assert {k: service[k] for k in ADDED_FIELDS} == SELF_RUN_FIELDS
        assert body["kafka"]["replicas"] == {"value": 3, "source": "kafka", "protected": False}

    def test_a_self_run_service_is_never_dialled_for_its_size(
        self, client, app, admin_headers, tmp_path, down_ch
    ):
        _wire_gitcrud(app, tmp_path)

        _by_service(client, admin_headers)

        assert down_ch.connections == 0

    def test_a_declared_external_mode_alone_changes_nothing(
        self, client, app, admin_headers, tmp_path, down_ch
    ):
        """No stated provider is today's deployment, whatever the repo declares."""
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"mode": "external"}}, "tester")

        assert _by_service(client, admin_headers)["kafka"]["managed_by"] == "dfe"

    def test_writes_keep_todays_answers(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")

        raised = client.put(
            f"{_BACKING}/overlays/kafka/vars/kafka.replicas",
            json={"value": 6},
            headers=admin_headers,
        )
        lowered = client.put(
            f"{_BACKING}/overlays/kafka/vars/kafka.replicas",
            json={"value": 2},
            headers=admin_headers,
        )

        assert raised.status_code == 200, raised.text
        assert lowered.status_code == 400
        assert lowered.json()["code"] == "scale_down_refused"


class TestStatedFacts:
    def test_the_env_facts_reach_the_body(
        self, client, app, admin_headers, tmp_path, down_ch, monkeypatch
    ):
        _wire_gitcrud(app, tmp_path)
        monkeypatch.setenv("DFE_DEPLOYMENT_KAFKA_PROVIDER", "confluent-cloud")
        monkeypatch.setenv("DFE_DEPLOYMENT_KAFKA_TIER", "basic")
        monkeypatch.setenv("DFE_DEPLOYMENT_KAFKA_REGION", "us-east-1")
        monkeypatch.setenv("DFE_DEPLOYMENT_CLICKHOUSE_PROVIDER", "clickhouse-cloud")
        monkeypatch.setenv("DFE_DEPLOYMENT_CLICKHOUSE_TIER", "scale")
        monkeypatch.setenv("DFE_DEPLOYMENT_CLICKHOUSE_REGION", "eu-west-1")
        app.state.settings.deployment.backing = backing_from_environment()

        body = _by_service(client, admin_headers)

        kafka, ch = body["kafka"], body["clickhouse"]
        assert (kafka["managed_by"], kafka["provider"], kafka["tier"], kafka["region"]) == (
            "provider",
            "confluent-cloud",
            "basic",
            "us-east-1",
        )
        assert kafka["read_only"] == KAFKA_READ_ONLY
        assert (ch["managed_by"], ch["provider"], ch["tier"], ch["region"]) == (
            "provider",
            "clickhouse-cloud",
            "scale",
            "eu-west-1",
        )
        assert ch["read_only"] == CLICKHOUSE_READ_ONLY
        assert kafka["limits"] == ch["limits"] == []
        assert kafka["principals"] == ch["principals"] == []

    def test_a_failed_size_read_is_null_and_was_attempted(
        self, client, app, admin_headers, tmp_path, down_ch
    ):
        """The broker guard refuses the admin client and the server drops the connection."""
        _wire_gitcrud(app, tmp_path)
        _state(app, kafka=CONFLUENT, ch=CH_EXTERNAL)

        body = _by_service(client, admin_headers)

        assert body["kafka"]["observed"] == UNKNOWN_SIZE
        assert body["clickhouse"]["observed"] == UNKNOWN_SIZE
        assert down_ch.connections >= 1

    def test_a_poll_inside_the_window_does_not_dial_again(
        self, client, app, admin_headers, tmp_path, down_ch
    ):
        """A console polls the card; the size is held per service rather than re-read."""
        _wire_gitcrud(app, tmp_path)
        _state(app, ch=CH_EXTERNAL)

        _by_service(client, admin_headers)
        dialled = down_ch.connections
        _by_service(client, admin_headers)
        client.get(f"{_BACKING}/clickhouse", headers=admin_headers)

        assert dialled >= 1
        assert down_ch.connections == dialled

    def test_one_service_managed_leaves_the_other_self_run(
        self, client, app, admin_headers, tmp_path, down_ch
    ):
        _wire_gitcrud(app, tmp_path)
        _state(app, kafka=MSK)

        body = _by_service(client, admin_headers)

        assert body["kafka"]["managed_by"] == "provider"
        assert {k: body["clickhouse"][k] for k in ADDED_FIELDS} == SELF_RUN_FIELDS
        assert down_ch.connections == 0

    def test_a_self_run_key_with_the_repo_declaring_external_is_external(
        self, client, app, admin_headers, tmp_path, down_ch
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"mode": "external"}}, "tester")
        _state(app, kafka=BackingFactSettings(provider="strimzi"))

        kafka = _by_service(client, admin_headers)["kafka"]

        assert (kafka["managed_by"], kafka["provider"]) == ("external", "strimzi")
        assert kafka["read_only"] == KAFKA_EXTERNAL

    def test_the_per_chart_mode_wins_over_common(
        self, client, app, admin_headers, tmp_path, down_ch
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"mode": "external"}}, "tester")
        gc.put("infravars", "kafka", {"kafka": {"mode": "cluster"}}, "tester")
        _state(app, kafka=BackingFactSettings(provider="strimzi"))

        assert _by_service(client, admin_headers)["kafka"]["managed_by"] == "dfe"

    def test_one_service_route_carries_the_same_facts(
        self, client, app, admin_headers, tmp_path, down_ch
    ):
        _wire_gitcrud(app, tmp_path)
        _state(app, ch=CH_EXTERNAL)

        one = client.get(f"{_BACKING}/clickhouse", headers=admin_headers).json()

        assert (one["managed_by"], one["provider"]) == ("external", "external")
        assert one["read_only"] == CLICKHOUSE_EXTERNAL


def _put(client, headers, overlay, path, value):
    return client.put(
        f"{_BACKING}/overlays/{overlay}/vars/{path}", json={"value": value}, headers=headers
    )


def _delete(client, headers, overlay, path):
    return client.delete(f"{_BACKING}/overlays/{overlay}/vars/{path}", headers=headers)


class TestManagedWritesAreRefused:
    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("kafka.mode", "cluster"),
            ("kafka.replicas", 9),
            ("kafka.storage", {"size": "1Ti"}),
            ("kafka.storage.size", "1Ti"),
            ("kafka.storageModel", "tiered-object"),
            ("kafka.resources.requests.cpu", "4"),
            ("kafka.objectStore.remoteKey", "k"),
            ("kafka.tieredObject.className", "x"),
            ("kafka.sizing.peakMbS", 100),
            ("kafka.controllerPool.replicas", 3),
            ("kafka", {"replicas": 9}),
        ],
    )
    def test_every_kafka_sizing_value_and_its_parents_refuse(
        self, client, app, admin_headers, tmp_path, path, value
    ):
        gc = _wire_gitcrud(app, tmp_path)
        _state(app, kafka=CONFLUENT)

        resp = _put(client, admin_headers, "kafka", path, value)

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "managed_by_provider"
        assert gc.list("infravars") == []

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("clickhouse.replicas", 3),
            ("clickhouse.keeper.replicas", 5),
            ("clickhouse.keeper", {"replicas": 5}),
            ("clickhouse.storage.storageClass", "gp3"),
            ("clickhouse.tieredBlock.coldSize", "4Ti"),
            ("clickhouse", {"replicas": 3}),
        ],
    )
    @pytest.mark.parametrize("stated", [CH_CLOUD, CH_EXTERNAL], ids=["provider", "external"])
    def test_every_clickhouse_sizing_value_refuses_for_provider_and_external(
        self, client, app, admin_headers, tmp_path, path, value, stated
    ):
        _wire_gitcrud(app, tmp_path)
        _state(app, ch=stated)

        resp = _put(client, admin_headers, "clickhouse-cluster", path, value)

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "managed_by_provider"

    @pytest.mark.parametrize(
        ("overlay", "path", "value"),
        [
            ("kafka", "kafka.name", "dfe-kafka"),
            ("kafka", "kafka.external.bootstrap", "b:9092"),
            ("clickhouse-cluster", "clickhouse.keeper.resources.limits.cpu", "2"),
            ("network-policies", "kafka.replicas", 1),
        ],
    )
    def test_a_value_outside_the_read_only_set_still_writes(
        self, client, app, admin_headers, tmp_path, overlay, path, value
    ):
        _wire_gitcrud(app, tmp_path)
        _state(app, kafka=CONFLUENT, ch=CH_CLOUD)

        resp = _put(client, admin_headers, overlay, path, value)

        assert resp.status_code == 200, resp.text

    def test_the_shared_overlay_is_guarded_as_the_per_chart_one_is(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        _state(app, kafka=MSK)

        resp = _put(client, admin_headers, "common", "kafka.replicas", 6)

        assert resp.status_code == 409, resp.text
        assert gc.list("infravars") == []

    def test_external_by_declared_mode_refuses(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"mode": "external"}}, "tester")
        _state(app, kafka=BackingFactSettings(provider="redpanda"))

        resp = _put(client, admin_headers, "kafka", "kafka.replicas", 3)

        assert resp.status_code == 409, resp.text
        assert resp.json()["context"]["managed_by"] == "external"

    def test_a_self_run_key_with_no_declared_mode_writes(
        self, client, app, admin_headers, tmp_path
    ):
        _wire_gitcrud(app, tmp_path)
        _state(
            app,
            kafka=BackingFactSettings(provider="strimzi"),
            ch=BackingFactSettings(provider="in_cluster"),
        )

        assert _put(client, admin_headers, "kafka", "kafka.replicas", 3).status_code == 200
        assert (
            _put(client, admin_headers, "clickhouse-cluster", "clickhouse.replicas", 3).status_code
            == 200
        )


class TestTheWayBackFromExternal:
    """A service the deploy repo made external by its mode goes back to DFE by its mode."""

    @staticmethod
    def _made_external(app, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"mode": "external", "replicas": 3}}, "tester")
        _state(app, kafka=BackingFactSettings(provider="strimzi"))
        return gc

    def test_writing_the_mode_back_hands_the_service_to_dfe(
        self, client, app, admin_headers, tmp_path
    ):
        gc = self._made_external(app, tmp_path)

        resp = _put(client, admin_headers, "common", "kafka.mode", "cluster")

        assert resp.status_code == 200, resp.text
        assert gc.get("infravars", "common")["kafka"]["mode"] == "cluster"
        assert _by_service(client, admin_headers)["kafka"]["managed_by"] == "dfe"

    def test_reverting_the_mode_hands_the_service_to_dfe(
        self, client, app, admin_headers, tmp_path
    ):
        gc = self._made_external(app, tmp_path)

        resp = _delete(client, admin_headers, "common", "kafka.mode")

        assert resp.status_code == 200, resp.text
        assert "mode" not in gc.get("infravars", "common")["kafka"]
        assert _by_service(client, admin_headers)["kafka"]["managed_by"] == "dfe"

    def test_sizing_stays_refused_while_it_is_external(self, client, app, admin_headers, tmp_path):
        """Only the mode is the way back: the whole map still carries the sizing."""
        gc = self._made_external(app, tmp_path)

        replicas = _put(client, admin_headers, "common", "kafka.replicas", 5)
        whole = _put(client, admin_headers, "common", "kafka", {"mode": "cluster"})

        assert (replicas.status_code, whole.status_code) == (409, 409)
        assert gc.get("infravars", "common") == {"kafka": {"mode": "external", "replicas": 3}}

    def test_a_provider_managed_mode_stays_refused(self, client, app, admin_headers, tmp_path):
        """No deploy-repo mode turns a SaaS broker into one DFE runs."""
        _wire_gitcrud(app, tmp_path)
        _state(app, kafka=MSK)

        resp = _put(client, admin_headers, "kafka", "kafka.mode", "cluster")

        assert resp.status_code == 409, resp.text
        assert "mode" in resp.json()["context"]["read_only"]


class TestTheRefusalBody:
    def test_a_provider_sized_in_the_deployment(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        _state(app, kafka=CONFLUENT)

        body = _put(client, admin_headers, "kafka", "kafka.replicas", 9).json()

        assert body["code"] == "managed_by_provider"
        assert body["message"].startswith("kafka.replicas is not this deployment's to set")
        assert "confluent-cloud sizes kafka on the basic tier" in body["message"]
        assert "deployment dial" in body["message"]
        assert body["context"] == {
            "service": "kafka",
            "path": "kafka.replicas",
            "managed_by": "provider",
            "provider": "confluent-cloud",
            "tier": "basic",
            "set_in": "deployment",
            "read_only": KAFKA_READ_ONLY,
        }

    def test_a_provider_sized_only_in_its_own_console(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        _state(app, ch=CH_CLOUD)

        body = _put(client, admin_headers, "clickhouse-cluster", "clickhouse.replicas", 3).json()

        assert body["context"]["set_in"] == "provider"
        assert body["context"]["read_only"] == CLICKHOUSE_READ_ONLY
        assert "provider's own console or API" in body["message"]
        # No tier stated, so the message claims none.
        assert " tier" not in body["message"]

    def test_an_external_service_is_changed_by_whoever_runs_it(
        self, client, app, admin_headers, tmp_path
    ):
        _wire_gitcrud(app, tmp_path)
        _state(app, ch=CH_EXTERNAL)

        body = _put(client, admin_headers, "clickhouse-cluster", "clickhouse.replicas", 3).json()

        assert body["context"]["managed_by"] == "external"
        assert body["context"]["set_in"] == "operator"
        assert "DFE does not run this clickhouse" in body["message"]


class TestTheGuardRunsFirst:
    def test_the_override_grant_does_not_lift_it(self, client, app, admin_headers, tmp_path):
        """admin holds '*', so helmvars:override, and is still refused: the write is inert."""
        gc = _wire_gitcrud(app, tmp_path)
        _state(app, kafka=CONFLUENT)

        resp = _put(client, admin_headers, "kafka", "kafka.storage.size", "1Ti")

        assert resp.status_code == 409, resp.text
        assert gc.list("infravars") == []

    def test_it_answers_before_the_policy_lock(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        _wire_gitcrud(app, tmp_path)
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "storage-layout", "protected": STORAGE_LOCK},
            headers=admin_headers,
        )
        _state(app, kafka=CONFLUENT)

        resp = _put(client, _writer(app, api_settings), "kafka", "kafka.storage.size", "1Ti")

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "managed_by_provider"

    def test_it_answers_before_the_scale_down_guard(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        _state(app, kafka=CONFLUENT)

        resp = _put(client, admin_headers, "kafka", "kafka.replicas", 2)

        assert resp.status_code == 409, resp.text
        assert gc.get("infravars", "kafka") == {"kafka": {"replicas": 5}}


class TestRevertsAreRefusedToo:
    @pytest.mark.parametrize("holds_override", [True, False], ids=["override", "writer"])
    def test_a_revert_of_a_read_only_value_refuses(
        self, client, app, api_settings, admin_headers, tmp_path, holds_override
    ):
        gc = _wire_gitcrud(app, tmp_path)
        stored = {"kafka": {"replicas": 5, "storage": {"size": "1Ti"}}}
        gc.put("infravars", "kafka", stored, "tester")
        _state(app, kafka=MSK)
        headers = admin_headers if holds_override else _writer(app, api_settings)

        for path in ("kafka.replicas", "kafka.storage", "kafka"):
            resp = _delete(client, headers, "kafka", path)
            assert resp.status_code == 409, f"{path}: {resp.text}"
            assert resp.json()["code"] == "managed_by_provider"
        assert gc.get("infravars", "kafka") == stored

    def test_a_revert_outside_the_read_only_set_goes_through(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"name": "dfe-kafka"}}, "tester")
        _state(app, kafka=MSK)

        resp = _delete(client, admin_headers, "kafka", "kafka.name")

        assert resp.status_code == 200, resp.text
        assert "name" not in gc.get("infravars", "kafka").get("kafka", {})


class TestSystemDeploymentFacts:
    def test_no_stated_provider_is_dfe_for_every_service(self, client, admin_headers):
        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["backing"] == {
            "clickhouse": {"managed_by": "dfe", "provider": None, "tier": None, "region": None},
            "kafka": {"managed_by": "dfe", "provider": None, "tier": None, "region": None},
        }

    def test_stated_facts_are_reported(self, client, app, admin_headers):
        _state(app, kafka=CONFLUENT, ch=CH_EXTERNAL)

        backing = client.get("/api/v1/system/deployment", headers=admin_headers).json()["backing"]

        assert backing["kafka"] == {
            "managed_by": "provider",
            "provider": "confluent-cloud",
            "tier": "basic",
            "region": "us-east-1",
        }
        assert backing["clickhouse"]["managed_by"] == "external"

    def test_the_declared_mode_is_read_where_the_key_leaves_it_open(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"mode": "external"}}, "tester")
        _state(app, kafka=BackingFactSettings(provider="strimzi"))

        backing = client.get("/api/v1/system/deployment", headers=admin_headers).json()["backing"]

        assert backing["kafka"]["managed_by"] == "external"

    def test_without_a_deploy_repo_nothing_is_declared(self, client, app, admin_headers):
        _state(app, kafka=BackingFactSettings(provider="strimzi"))

        resp = client.get("/api/v1/system/deployment", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["backing"]["kafka"]["managed_by"] == "dfe"

    def test_a_viewer_reads_the_facts(self, client, app, viewer_headers):
        _state(app, kafka=MSK)

        resp = client.get("/api/v1/system/deployment", headers=viewer_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["backing"]["kafka"]["provider"] == "msk"
