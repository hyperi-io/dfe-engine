"""Tests for the backing-services surface: declared reads, protected writes.

The two things worth proving are that a locked key refuses with the policy that
locked it, and that the lock is narrow enough to leave the rest of the same file
writable. Everything runs against a real dulwich repo, no mocks.
"""

import copy
from importlib import resources

import pytest

from dfe_engine.appmgmt import contract
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore
from dfe_engine.yaml_utils import yaml_load_string

# The SHIPPED policy, not a copy of it: a pattern added to the seeded file has to
# take effect here without anyone remembering to retype it.
SHIPPED_POLICY = yaml_load_string(
    resources.files("dfe_engine.governance.resources.policies")
    .joinpath("storage-layout.yaml")
    .read_text(encoding="utf-8")
)
STORAGE_LOCK: list[str] = SHIPPED_POLICY["protected"]
SIZING_LOCK: list[str] = yaml_load_string(
    resources.files("dfe_engine.governance.resources.policies")
    .joinpath("sizing-locks.yaml")
    .read_text(encoding="utf-8")
)["protected"]


def _wire_gitcrud(app, tmp_path):
    """A real local-repo GitCrud under a dev posture (direct commit is sanctioned)."""
    app.state.settings.env = "dev"
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


def _lock(client, admin_headers):
    client.post(
        "/api/v1/governance/admin/policies",
        json={"name": "storage-layout", "protected": STORAGE_LOCK},
        headers=admin_headers,
    )


def _writer(app, api_settings):
    """A caller with helmvars:write but NOT helmvars:override."""
    from tests.unit.test_api.test_governed_ops import _scoped_headers

    return _scoped_headers(
        app,
        api_settings,
        username="infra-writer",
        role="infra-writer",
        permissions=["helmvars:read", "helmvars:write"],
    )


class TestDeclaredReads:
    def test_503_when_gitops_not_configured(self, client, admin_headers):
        resp = client.get("/api/v1/backing-services", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_undeclared_values_report_no_source(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        body = client.get("/api/v1/backing-services", headers=admin_headers).json()
        assert [s["service"] for s in body] == ["clickhouse", "kafka"]
        ch = body[0]
        assert ch["mode"] == {"value": None, "source": None, "protected": False}
        assert ch["storage_model"]["value"] is None
        assert ch["overlay"] == "clickhouse-cluster.yaml"

    def test_per_chart_overlay_beats_common(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"clickhouse": {"mode": "single"}}, "tester")
        gc.put(
            "infravars",
            "clickhouse-cluster",
            {"clickhouse": {"mode": "external", "storage": {"size": "500Gi"}}},
            "tester",
        )
        ch = client.get("/api/v1/backing-services/clickhouse", headers=admin_headers).json()
        assert ch["mode"] == {
            "value": "external",
            "source": "clickhouse-cluster",
            "protected": False,
        }
        assert ch["storage_size"]["value"] == "500Gi"

    def test_common_alone_is_reported_as_the_source(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"mode": "external"}}, "tester")
        kafka = client.get("/api/v1/backing-services/kafka", headers=admin_headers).json()
        assert kafka["mode"] == {"value": "external", "source": "common", "protected": False}

    def test_resources_are_reported_per_leaf(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put(
            "infravars",
            "kafka",
            {"kafka": {"resources": {"requests": {"cpu": "2"}}, "replicas": 5}},
            "tester",
        )
        kafka = client.get("/api/v1/backing-services/kafka", headers=admin_headers).json()
        assert kafka["resources"]["resources.requests.cpu"]["value"] == "2"
        assert kafka["resources"]["resources.limits.memory"]["value"] is None
        assert kafka["replicas"]["value"] == 5

    def test_protected_flag_rides_the_read(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        _lock(client, admin_headers)
        gc.put("infravars", "clickhouse-cluster", {"clickhouse": {"mode": "external"}}, "tester")
        ch = client.get("/api/v1/backing-services/clickhouse", headers=admin_headers).json()
        assert ch["mode"]["protected"] is True
        assert ch["replicas"]["protected"] is False

    def test_unknown_service_is_404(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.get("/api/v1/backing-services/postgres", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_every_service_carries_its_values_prefix(self, client, app, admin_headers, tmp_path):
        # ClickHouse's chart name is not its prefix, so a caller deriving one from
        # service or chart gets it wrong for one of the two.
        _wire_gitcrud(app, tmp_path)
        body = client.get("/api/v1/backing-services", headers=admin_headers).json()
        assert {s["service"]: s["prefix"] for s in body} == {
            "clickhouse": "clickhouse",
            "kafka": "kafka",
        }
        assert all(s["prefix"] for s in body)
        assert [s for s in body if s["chart"] != s["service"]]

    def test_the_prefix_is_the_one_writes_actually_use(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        ch = client.get("/api/v1/backing-services/clickhouse", headers=admin_headers).json()
        path = f"{ch['prefix']}.replicas"
        resp = client.put(
            f"/api/v1/backing-services/overlays/{ch['chart']}/vars/{path}",
            json={"value": 7},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert gc.get("infravars", ch["chart"])["clickhouse"]["replicas"] == 7
        reread = client.get("/api/v1/backing-services/clickhouse", headers=admin_headers).json()
        assert reread["replicas"]["value"] == 7

    def test_viewer_cannot_read(self, client, app, viewer_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        assert client.get("/api/v1/backing-services", headers=viewer_headers).status_code == 403


class TestOverlayVars:
    def test_set_then_list(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.put(
            "/api/v1/backing-services/overlays/clickhouse-cluster/vars/clickhouse.replicas",
            json={"value": 5},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["commit_sha"]

        listed = client.get(
            "/api/v1/backing-services/overlays/clickhouse-cluster/vars", headers=admin_headers
        ).json()
        assert {v["path"]: v["value"] for v in listed} == {"clickhouse.replicas": 5}
        names = client.get("/api/v1/backing-services/overlays", headers=admin_headers).json()
        assert names == ["clickhouse-cluster"]

    def test_credentials_come_back_masked(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put(
            "infravars",
            "clickhouse-cluster",
            {
                "clickhouse": {"replicas": 3, "auth": {"password": "ch-pw"}},
                "extraEnv": {"CLICKHOUSE_ADMIN_SECRET": "ch-secret"},
            },
            "tester",
        )
        resp = client.get(
            "/api/v1/backing-services/overlays/clickhouse-cluster/vars", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert {v["path"]: v["value"] for v in resp.json()} == {
            "clickhouse.replicas": 3,
            "clickhouse.auth.password": contract.REDACTED,
            "extraEnv.CLICKHOUSE_ADMIN_SECRET": contract.REDACTED,
        }
        assert "ch-pw" not in resp.text
        assert "ch-secret" not in resp.text
        # The chart still reads the real value.
        assert gc.get("infravars", "clickhouse-cluster")["clickhouse"]["auth"]["password"] == (
            "ch-pw"
        )

    def test_writes_land_in_the_infra_directory(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        client.put(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.replicas",
            json={"value": 5},
            headers=admin_headers,
        )
        # Not values/, which is where the app appset's glob would find it and
        # spawn a phantom Argo application.
        assert (gc.repo_path / "infra" / "kafka.yaml").is_file()
        assert not (gc.repo_path / "values" / "kafka.yaml").exists()

    def test_viewer_cannot_write(self, client, app, viewer_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.put(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.replicas",
            json={"value": 5},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestReloadHint:
    """Every write says what syncing it does, so the UI stops implying a restart."""

    def _put(self, client, headers, path, value, overlay="kafka"):
        return client.put(
            f"/api/v1/backing-services/overlays/{overlay}/vars/{path}",
            json={"value": value},
            headers=headers,
        )

    def test_resources_roll_the_pods(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = self._put(client, admin_headers, "kafka.resources.requests.cpu", "2")
        assert resp.status_code == 200, resp.text
        assert resp.json()["reload"] == "roll"

    def test_a_member_count_applies_without_a_restart(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        assert self._put(client, admin_headers, "kafka.replicas", 5).json()["reload"] == "apply"

    def test_storage_needs_the_statefulset_recreated(self, client, app, admin_headers, tmp_path):
        # volumeClaimTemplates are immutable, so no sync can apply this on its own.
        _wire_gitcrud(app, tmp_path)
        resp = self._put(client, admin_headers, "kafka.storage.size", "200Gi")
        assert resp.json()["reload"] == "recreate"

    def test_a_mode_change_redeploys(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        assert self._put(client, admin_headers, "kafka.mode", "external").json()["reload"] == (
            "redeploy"
        )

    def test_the_kafka_storage_model_rolls_rather_than_recreating(
        self, client, app, admin_headers, tmp_path
    ):
        # `storage` and `storageModel` are different keys with different answers,
        # and Strimzi reconciles tieredStorage onto the running CR.
        _wire_gitcrud(app, tmp_path)
        assert self._put(client, admin_headers, "kafka.storageModel", "tiered-object").json()[
            "reload"
        ] == ("roll")

    def test_the_clickhouse_storage_model_needs_a_recreate(
        self, client, app, admin_headers, tmp_path
    ):
        """The same segment, the opposite verdict: the ClickHouse operator takes no
        new disk on an existing cluster, so the write Strimzi rolls rebuilds here."""
        _wire_gitcrud(app, tmp_path)
        resp = self._put(
            client,
            admin_headers,
            "clickhouse.storageModel",
            "tiered-block",
            overlay="clickhouse-cluster",
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["reload"] == "recreate"

    def test_the_clickhouse_cold_tier_needs_a_recreate(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = self._put(
            client,
            admin_headers,
            "clickhouse.tieredBlock.coldSize",
            "4Ti",
            overlay="clickhouse-cluster",
        )
        assert resp.json()["reload"] == "recreate"

    def test_the_object_store_block_rolls_on_both_services(
        self, client, app, admin_headers, tmp_path
    ):
        """Credential binding and request bounds are server or broker config, so
        both services restart pods and nothing more."""
        _wire_gitcrud(app, tmp_path)
        ch = self._put(
            client,
            admin_headers,
            "clickhouse.objectStore.endpoint",
            "https://b.example.com/ch/",
            overlay="clickhouse-cluster",
        )
        assert ch.json()["reload"] == "roll", ch.text
        kafka = self._put(client, admin_headers, "kafka.objectStore.remoteKey", "services/minio")
        assert kafka.json()["reload"] == "roll", kafka.text

    def test_the_kafka_plugin_block_rolls(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = self._put(client, admin_headers, "kafka.tieredObject.className", "com.example.Rsm")
        assert resp.json()["reload"] == "roll"

    def test_an_unmapped_key_reports_apply(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        assert self._put(client, admin_headers, "kafka.name", "dfe-kafka").json()["reload"] == (
            "apply"
        )

    def test_a_revert_carries_the_hint_too(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"resources": {"requests": {"cpu": "2"}}}}, "t")
        resp = client.delete(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.resources.requests.cpu",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["reload"] == "roll"


class TestMemberCountsGoUpOnly:
    """Scale-down loses data rather than capacity, so it is refused server-side."""

    def _set(self, client, headers, name, path, value):
        return client.put(
            f"/api/v1/backing-services/overlays/{name}/vars/{path}",
            json={"value": value},
            headers=headers,
        )

    def test_lowering_kafka_brokers_is_refused_with_the_reason(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        resp = self._set(client, admin_headers, "kafka", "kafka.replicas", 3)
        assert resp.status_code == 400
        assert resp.json()["code"] == "scale_down_refused"
        assert "reassigned off it" in resp.json()["message"]
        assert gc.get("infravars", "kafka")["kafka"]["replicas"] == 5

    def test_lowering_clickhouse_nodes_names_the_clickhouse_reason(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "clickhouse-cluster", {"clickhouse": {"replicas": 3}}, "tester")
        resp = self._set(client, admin_headers, "clickhouse-cluster", "clickhouse.replicas", 1)
        assert resp.status_code == 400
        assert "drops a copy of the data" in resp.json()["message"]

    def test_the_keeper_ensemble_is_up_only_as_well(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put(
            "infravars", "clickhouse-cluster", {"clickhouse": {"keeper": {"replicas": 3}}}, "tester"
        )
        resp = self._set(
            client, admin_headers, "clickhouse-cluster", "clickhouse.keeper.replicas", 1
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "scale_down_refused"

    def test_raising_a_count_is_accepted(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 3}}, "tester")
        assert self._set(client, admin_headers, "kafka", "kafka.replicas", 6).status_code == 200
        assert gc.get("infravars", "kafka")["kafka"]["replicas"] == 6

    def test_the_same_count_is_accepted(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 3}}, "tester")
        assert self._set(client, admin_headers, "kafka", "kafka.replicas", 3).status_code == 200

    def test_an_undeclared_count_accepts_anything(self, client, app, admin_headers, tmp_path):
        # Nothing declared means nothing to compare against; the chart or profile
        # default is not readable from here.
        _wire_gitcrud(app, tmp_path)
        assert self._set(client, admin_headers, "kafka", "kafka.replicas", 1).status_code == 200

    def test_cpu_and_memory_move_down_freely(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put(
            "infravars",
            "kafka",
            {"kafka": {"resources": {"requests": {"cpu": "4", "memory": "8Gi"}}}},
            "tester",
        )
        assert (
            self._set(
                client, admin_headers, "kafka", "kafka.resources.requests.cpu", "1"
            ).status_code
            == 200
        )
        assert (
            self._set(
                client, admin_headers, "kafka", "kafka.resources.requests.memory", "1Gi"
            ).status_code
            == 200
        )

    def test_the_comparison_is_against_the_resolved_stack_not_one_file(
        self, client, app, admin_headers, tmp_path
    ):
        # The per-chart file wins, so lowering the shared file below it changes
        # nothing and must not be refused.
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"replicas": 5}}, "tester")
        gc.put("infravars", "kafka", {"kafka": {"replicas": 9}}, "tester")
        assert self._set(client, admin_headers, "common", "kafka.replicas", 2).status_code == 200
        assert gc.get("infravars", "common")["kafka"]["replicas"] == 2

    def test_lowering_the_winning_file_is_still_refused(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "common", {"kafka": {"replicas": 5}}, "tester")
        gc.put("infravars", "kafka", {"kafka": {"replicas": 9}}, "tester")
        resp = self._set(client, admin_headers, "kafka", "kafka.replicas", 6)
        assert resp.status_code == 400
        assert "9 -> 6" in resp.json()["message"]

    def test_an_unrelated_overlay_is_not_guarded(self, client, app, admin_headers, tmp_path):
        # network-policies is not in either service's stack, so a count written
        # there cannot lower anything.
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 9}}, "tester")
        assert (
            self._set(client, admin_headers, "network-policies", "kafka.replicas", 1).status_code
            == 200
        )

    def test_the_override_grant_does_not_bypass_the_data_loss_guard(
        self, client, app, admin_headers, tmp_path
    ):
        # This is not a policy lock: admin holds helmvars:override and is still
        # refused, because the hazard is data loss rather than governance.
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        assert self._set(client, admin_headers, "kafka", "kafka.replicas", 2).status_code == 400

    @pytest.mark.parametrize(
        ("name", "stored", "path", "value", "count"),
        [
            (
                "clickhouse-cluster",
                {"clickhouse": {"keeper": {"replicas": 3}}},
                "clickhouse.keeper",
                {"replicas": 1},
                "clickhouse.keeper.replicas",
            ),
            ("kafka", {"kafka": {"replicas": 5}}, "kafka", {"replicas": 2}, "kafka.replicas"),
        ],
    )
    def test_a_parent_map_cannot_lower_a_count_below_it(
        self, client, app, admin_headers, tmp_path, name, stored, path, value, count
    ):
        """The map replaces the count it carries, so the guard compares that count."""
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", name, stored, "tester")
        resp = self._set(client, admin_headers, name, path, value)
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "scale_down_refused"
        assert count in resp.json()["message"]
        assert gc.get("infravars", name) == stored

    def test_a_writer_cannot_shrink_the_keeper_with_a_map_that_omits_it(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        """No shipped policy locks the keeper, so this guard is all a helmvars:write holder meets."""
        gc = _wire_gitcrud(app, tmp_path)
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "shipped", "protected": [*STORAGE_LOCK, *SIZING_LOCK]},
            headers=admin_headers,
        )
        stored = {"clickhouse": {"keeper": {"replicas": 5, "resources": {"limits": {"cpu": "1"}}}}}
        gc.put("infravars", "clickhouse-cluster", stored, "tester")
        resp = self._set(
            client,
            _writer(app, api_settings),
            "clickhouse-cluster",
            "clickhouse.keeper",
            {"resources": {"limits": {"cpu": "2"}}},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "scale_down_refused"
        assert "clickhouse.keeper.replicas" in resp.json()["message"]
        assert "chart default" in resp.json()["message"]
        assert gc.get("infravars", "clickhouse-cluster") == stored

    @pytest.mark.parametrize(
        ("name", "stored", "path", "value", "said"),
        [
            ("kafka", {"kafka": {"replicas": 5}}, "kafka", {"x": 1}, "chart default"),
            ("kafka", {"kafka": {"replicas": 5}}, "kafka.replicas", None, "chart default"),
            ("kafka", {"kafka": {"replicas": 5}}, "kafka.replicas", "7", "something else"),
            (
                "clickhouse-cluster",
                {"clickhouse": {"replicas": 3, "keeper": {"replicas": 3}}},
                "clickhouse.keeper",
                {},
                "chart default",
            ),
        ],
    )
    def test_a_declared_count_cannot_be_written_away(
        self, client, app, admin_headers, tmp_path, name, stored, path, value, said
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", name, stored, "tester")
        resp = self._set(client, admin_headers, name, path, value)
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "scale_down_refused"
        assert said in resp.json()["message"]
        assert gc.get("infravars", name) == stored

    def test_a_parent_map_that_keeps_the_count_still_writes(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"replicas": 5}}, "tester")
        resp = self._set(client, admin_headers, "kafka", "kafka", {"replicas": 5, "x": 1})
        assert resp.status_code == 200, resp.text


class TestStorageModelIsDecidedAtDeploy:
    def test_storage_model_write_is_refused_with_the_policy(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        _wire_gitcrud(app, tmp_path)
        _lock(client, admin_headers)
        resp = client.put(
            "/api/v1/backing-services/overlays/clickhouse-cluster/vars/clickhouse.storageModel",
            json={"value": "cached-object"},
            headers=_writer(app, api_settings),
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "protected_var"
        # The reason names the var AND the pattern, so the UI can render it.
        assert "clickhouse.storageModel" in resp.json()["message"]
        assert "infravars:*:clickhouse.storageModel" in resp.json()["message"]

    def test_every_locked_key_refuses(self, client, app, api_settings, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        _lock(client, admin_headers)
        writer = _writer(app, api_settings)
        locked = [
            ("clickhouse-cluster", "clickhouse.mode", "external"),
            ("clickhouse-cluster", "clickhouse.storageModel", "cached-object"),
            ("clickhouse-cluster", "clickhouse.objectStore.endpoint", "https://b.example.com/ch/"),
            ("clickhouse-cluster", "clickhouse.objectStore.supportBatchDelete", False),
            ("clickhouse-cluster", "clickhouse.tieredBlock.coldName", "slower"),
            ("clickhouse-cluster", "clickhouse.tieredBlock.coldStorageClass", "gp3"),
            ("clickhouse-cluster", "clickhouse.tieredBlock.coldSize", "8Ti"),
            ("clickhouse-cluster", "clickhouse.storage.size", "500Gi"),
            ("clickhouse-cluster", "clickhouse.storage.storageClass", "gp3"),
            ("kafka", "kafka.mode", "external"),
            ("kafka", "kafka.storageModel", "tiered-object"),
            ("kafka", "kafka.tieredObject.className", "com.example.Rsm"),
            ("kafka", "kafka.objectStore.remoteKey", "services/minio"),
            ("kafka", "kafka.storage.size", "200Gi"),
            ("kafka", "kafka.storage.storageClass", "gp3"),
        ]
        for name, path, value in locked:
            resp = client.put(
                f"/api/v1/backing-services/overlays/{name}/vars/{path}",
                json={"value": value},
                headers=writer,
            )
            assert resp.status_code == 403, f"{path} was writable: {resp.text}"
            assert resp.json()["code"] == "protected_var"

    @pytest.mark.parametrize(
        "path",
        [
            "clickhouse.storage.size",
            "clickhouse.storage.storageClass",
            "kafka.storage.size",
            "kafka.storage.storageClass",
        ],
    )
    def test_the_shipped_policy_locks_the_disk(self, path):
        # volumeClaimTemplates are immutable, so a
        # size or class change is a StatefulSet recreate rather than a values edit.
        assert f"infravars:*:{path}" in STORAGE_LOCK

    @pytest.mark.parametrize(
        "path",
        [
            "clickhouse.objectStore.*",
            "clickhouse.tieredBlock.*",
            "kafka.objectStore.*",
            "kafka.tieredObject.*",
        ],
    )
    def test_the_shipped_policy_locks_every_dial_block(self, path):
        """One block per storage family, both services, so a new dial inside a block
        is locked the day it ships rather than the day someone remembers it."""
        assert f"infravars:*:{path}" in STORAGE_LOCK

    @pytest.mark.parametrize("path", ["clickhouse.s3.*", "clickhouse.tiered.*", "kafka.tiered.*"])
    def test_the_pre_vocabulary_spellings_stay_locked(self, path):
        """A deploy repo pinned to an older chart still writes these paths."""
        assert f"infravars:*:{path}" in STORAGE_LOCK

    def test_storage_size_is_refused_for_a_writer_and_written_with_override(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        _lock(client, admin_headers)
        refused = client.put(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.storage.size",
            json={"value": "200Gi"},
            headers=_writer(app, api_settings),
        )
        assert refused.status_code == 403
        assert refused.json()["code"] == "protected_var"
        # admin holds '*' -> helmvars:override, which is the deliberate exception.
        allowed = client.put(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.storage.size",
            json={"value": "200Gi"},
            headers=admin_headers,
        )
        assert allowed.status_code == 200, allowed.text
        assert gc.get("infravars", "kafka")["kafka"]["storage"]["size"] == "200Gi"

    def test_the_lock_covers_common_yaml_too(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        """The pattern is infravars:*:..., so declaring a mode in the shared file
        is refused exactly as the per-chart file is."""
        _wire_gitcrud(app, tmp_path)
        _lock(client, admin_headers)
        resp = client.put(
            "/api/v1/backing-services/overlays/common/vars/kafka.mode",
            json={"value": "external"},
            headers=_writer(app, api_settings),
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "protected_var"

    def test_an_unprotected_key_in_the_same_file_still_writes(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        _lock(client, admin_headers)
        writer = _writer(app, api_settings)
        blocked = client.put(
            "/api/v1/backing-services/overlays/clickhouse-cluster/vars/clickhouse.storageModel",
            json={"value": "cached-object"},
            headers=writer,
        )
        assert blocked.status_code == 403
        allowed = client.put(
            "/api/v1/backing-services/overlays/clickhouse-cluster/vars/clickhouse.replicas",
            json={"value": 5},
            headers=writer,
        )
        assert allowed.status_code == 200, allowed.text
        doc = gc.get("infravars", "clickhouse-cluster")
        assert doc["clickhouse"]["replicas"] == 5
        assert "storageModel" not in doc["clickhouse"]

    def test_override_grant_still_gets_through(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        _lock(client, admin_headers)
        # admin holds '*' -> helmvars:override
        resp = client.put(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.storageModel",
            json={"value": "tiered-object"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text

    def test_reverting_a_locked_var_is_refused_too(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        """Deleting a protected var reverts it to the chart default, which changes
        it as surely as setting it does."""
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"storageModel": "tiered-object"}}, "tester")
        _lock(client, admin_headers)
        resp = client.delete(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.storageModel",
            headers=_writer(app, api_settings),
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "protected_var"
        assert gc.get("infravars", "kafka")["kafka"]["storageModel"] == "tiered-object"


class TestALockHoldsAboveAndBelowTheLeaf:
    """A set or revert at a parent replaces every leaf below it, and a set below a
    scalar turns it into a map, so both are refused like the leaf itself."""

    STORED = {"kafka": {"replicas": 3, "sizing": {"peakMbS": 50, "retentionHours": 24}}}

    def _setup(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", copy.deepcopy(self.STORED), "tester")
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "sizing-locks", "protected": SIZING_LOCK},
            headers=admin_headers,
        )
        return gc

    def test_the_locked_leaf_is_refused(self, client, app, api_settings, admin_headers, tmp_path):
        gc = self._setup(client, app, admin_headers, tmp_path)
        resp = client.put(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.sizing.peakMbS",
            json={"value": 999},
            headers=_writer(app, api_settings),
        )
        assert resp.status_code == 403, resp.text
        assert gc.get("infravars", "kafka") == self.STORED

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("kafka.sizing", {"peakMbS": 999, "retentionHours": 24}),
            ("kafka.sizing", {}),
            ("kafka", {"replicas": 3, "sizing": {"peakMbS": 999}}),
            ("kafka", {"replicas": 3}),
        ],
    )
    def test_a_map_written_at_a_parent_is_refused(
        self, client, app, api_settings, admin_headers, tmp_path, path, value
    ):
        gc = self._setup(client, app, admin_headers, tmp_path)
        resp = client.put(
            f"/api/v1/backing-services/overlays/kafka/vars/{path}",
            json={"value": value},
            headers=_writer(app, api_settings),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "protected_var"
        assert "infravars:*:kafka.sizing.*" in resp.json()["message"]
        assert gc.get("infravars", "kafka") == self.STORED

    @pytest.mark.parametrize("path", ["kafka.sizing", "kafka"])
    def test_reverting_a_parent_is_refused(
        self, client, app, api_settings, admin_headers, tmp_path, path
    ):
        gc = self._setup(client, app, admin_headers, tmp_path)
        resp = client.delete(
            f"/api/v1/backing-services/overlays/kafka/vars/{path}",
            headers=_writer(app, api_settings),
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "protected_var"
        assert gc.get("infravars", "kafka") == self.STORED

    def test_a_set_below_a_locked_scalar_is_refused(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        gc = self._setup(client, app, admin_headers, tmp_path)
        gc.put("infravars", "common", {"cloud": "aws"}, "tester")
        resp = client.put(
            "/api/v1/backing-services/overlays/common/vars/cloud.provider",
            json={"value": "gcp"},
            headers=_writer(app, api_settings),
        )
        assert resp.status_code == 403, resp.text
        assert "infravars:*:cloud" in resp.json()["message"]
        assert gc.get("infravars", "common") == {"cloud": "aws"}

    def test_a_sibling_under_the_same_parent_still_writes(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        gc = self._setup(client, app, admin_headers, tmp_path)
        resp = client.put(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.replicas",
            json={"value": 5},
            headers=_writer(app, api_settings),
        )
        assert resp.status_code == 200, resp.text
        doc = gc.get("infravars", "kafka")
        assert doc["kafka"]["replicas"] == 5
        assert doc["kafka"]["sizing"] == self.STORED["kafka"]["sizing"]

    def test_the_override_grant_still_writes_the_parent(self, client, app, admin_headers, tmp_path):
        gc = self._setup(client, app, admin_headers, tmp_path)
        # admin holds '*' -> helmvars:override
        resp = client.put(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.sizing",
            json={"value": {"peakMbS": 999}},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert gc.get("infravars", "kafka")["kafka"]["sizing"] == {"peakMbS": 999}

    def test_the_override_grant_still_reverts_the_parent(
        self, client, app, admin_headers, tmp_path
    ):
        gc = self._setup(client, app, admin_headers, tmp_path)
        resp = client.delete(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.sizing",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert "sizing" not in gc.get("infravars", "kafka")["kafka"]

    def test_the_vars_listing_marks_a_leaf_under_a_locked_scalar(
        self, client, app, admin_headers, tmp_path
    ):
        """The read flag and the write refusal come from one matcher, so they agree."""
        gc = self._setup(client, app, admin_headers, tmp_path)
        gc.put(
            "infravars", "common", {"cloud": {"provider": "aws"}, "networkModel": "open"}, "tester"
        )
        listed = client.get(
            "/api/v1/backing-services/overlays/common/vars", headers=admin_headers
        ).json()
        assert {v["path"]: v["protected"] for v in listed} == {
            "cloud.provider": True,
            "networkModel": False,
        }
