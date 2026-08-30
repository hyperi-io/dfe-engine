"""Tests for the backing-services surface: declared reads, protected writes.

The two things worth proving are that a locked key refuses with the policy that
locked it, and that the lock is narrow enough to leave the rest of the same file
writable. Everything runs against a real dulwich repo, no mocks.
"""

from __future__ import annotations

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore

STORAGE_LOCK = [
    "infravars:*:clickhouse.mode",
    "infravars:*:clickhouse.storageModel",
    "infravars:*:clickhouse.s3.*",
    "infravars:*:kafka.mode",
    "infravars:*:kafka.storageModel",
    "infravars:*:kafka.tiered.*",
]


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
        json={"name": "storage-model", "protected": STORAGE_LOCK},
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


class TestStorageModelIsDecidedAtDeploy:
    def test_storage_model_write_is_refused_with_the_policy(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        _wire_gitcrud(app, tmp_path)
        _lock(client, admin_headers)
        resp = client.put(
            "/api/v1/backing-services/overlays/clickhouse-cluster/vars/clickhouse.storageModel",
            json={"value": "s3backed"},
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
            ("clickhouse-cluster", "clickhouse.storageModel", "s3backed"),
            ("clickhouse-cluster", "clickhouse.s3.endpoint", "https://b.example.com/ch/"),
            ("kafka", "kafka.mode", "external"),
            ("kafka", "kafka.storageModel", "tiered"),
            ("kafka", "kafka.tiered.className", "com.example.Rsm"),
        ]
        for name, path, value in locked:
            resp = client.put(
                f"/api/v1/backing-services/overlays/{name}/vars/{path}",
                json={"value": value},
                headers=writer,
            )
            assert resp.status_code == 403, f"{path} was writable: {resp.text}"
            assert resp.json()["code"] == "protected_var"

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
            json={"value": "s3backed"},
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
            json={"value": "tiered"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text

    def test_reverting_a_locked_var_is_refused_too(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        """Deleting a protected var reverts it to the chart default, which changes
        it as surely as setting it does."""
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("infravars", "kafka", {"kafka": {"storageModel": "tiered"}}, "tester")
        _lock(client, admin_headers)
        resp = client.delete(
            "/api/v1/backing-services/overlays/kafka/vars/kafka.storageModel",
            headers=_writer(app, api_settings),
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "protected_var"
        assert gc.get("infravars", "kafka")["kafka"]["storageModel"] == "tiered"
