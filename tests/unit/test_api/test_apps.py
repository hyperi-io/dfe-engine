#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_apps.py
#  Purpose:      Tests for the app-management router
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""End-to-end router behaviour against a real local deploy repo."""

from __future__ import annotations

from dataclasses import replace

import pytest

from dfe_engine.appmgmt import catalogue
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore

VRL = "dfe-transform-vrl"
BASE = f"/api/v1/apps/{VRL}/edge"

VRL_SOURCE = ". = parse_json!(.message)\n.ts = to_timestamp!(.timestamp)\n"


def _wire(app, tmp_path, target: str = "kubernetes"):
    """Attach a real local-repo GitCrud and pick a deploy target."""
    app.state.settings.env = "dev"
    app.state.settings.deployment.target = target
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


def _define_source(client, headers, name: str, transform: dict | None = None):
    """Define the source a source-bound instance is named for.

    An instance of a source-bound app IS a source's processing step, so the
    source has to exist first. Idempotent: a repeat is a 409 the caller ignores.
    """
    body: dict = {"source": name, "match": {"field": "tags.collector.type", "value": name}}
    if transform is not None:
        body["transform"] = transform
    return client.post("/api/v1/sources", json=body, headers=headers)


def _define_fetcher_source(client, headers, name: str):
    """Define a fetcher-based source: a fetcher instance is named for one."""
    return client.post(
        "/api/v1/sources",
        json={
            "source": name,
            "fetcher": {"source_type": "crates_io", "config": {"crates": ["dfe-fetcher"]}},
        },
        headers=headers,
    )


def _live_source(client, headers, name: str, transform: dict | None = None) -> bool:
    """Define a source and mark it deployed, as a source deploy leaves it.

    A transform or fetcher instance is derived state: it exists for a source that
    is active, deployed, and names that app. Returns whether the source is there,
    so a test using an illegal name still exercises the route it meant to.
    """
    from dfe_engine.api.deps import _registries

    if _define_source(client, headers, name, transform).status_code != 201:
        return False
    _registries["source"].set_deployed_version(name, "1.0.0")
    return True


def _deploy(client, headers, instance: str = "edge", values: dict | None = None):
    """Deploy a transform instance the way the source deploy does."""
    _live_source(client, headers, instance, {"engine": "vrl"})
    return client.post(
        f"/api/v1/apps/{VRL}/instances",
        json={"instance": instance, "values": values or {}},
        headers=headers,
    )


class TestCatalogue:
    def test_503_when_gitops_not_configured(self, client, admin_headers):
        resp = client.get("/api/v1/apps", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_lists_every_app_with_no_instances(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.get("/api/v1/apps", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        by_service = {e["service"]: e for e in resp.json()}
        assert VRL in by_service
        assert by_service[VRL]["instances"] == []
        assert by_service[VRL]["file_sets"][0]["language"] == "vrl"
        assert by_service["dfe-transform-elastic"]["file_sets"] == []

    def test_the_routing_flag_matches_the_manifest(self, client, app, admin_headers, tmp_path):
        # Without it the UI can only find out by probing /routing for a 400.
        _wire(app, tmp_path)
        listed = client.get("/api/v1/apps", headers=admin_headers).json()
        assert {e["service"]: e["has_compiled_routing"] for e in listed} == {
            service: catalogue.descriptor(service).has_compiled_routing
            for service in catalogue.services()
        }

    def test_the_flag_predicts_what_the_routing_route_answers(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        client.post(
            "/api/v1/apps/dfe-archiver/instances",
            json={"instance": "default"},
            headers=admin_headers,
        )
        flagged = {
            e["service"]: e["has_compiled_routing"]
            for e in client.get("/api/v1/apps", headers=admin_headers).json()
        }
        assert flagged["dfe-archiver"] is False
        probe = client.get("/api/v1/apps/dfe-archiver/default/routing", headers=admin_headers)
        assert probe.status_code == 400
        assert probe.json()["code"] == "routing_not_compiled"

    def test_the_instance_summary_carries_the_flag(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        got = client.get(BASE, headers=admin_headers).json()
        assert got["has_compiled_routing"] is catalogue.descriptor(VRL).has_compiled_routing


class TestOptionalApps:
    """An app a deployment may run without, and the profiles it is offered in."""

    @pytest.fixture
    def optional_archiver(self, monkeypatch):
        """The archiver, made optional, so the shipped manifest stays the assertion."""
        replaced = replace(
            catalogue.descriptor("dfe-archiver"),
            profiles=frozenset({"scale", "scale-mesh"}),
            default_in=frozenset(),
        )
        monkeypatch.setitem(catalogue.APP_CATALOGUE, "dfe-archiver", replaced)

    def test_a_core_app_is_deployed_wherever_it_may_be(self, client, app, admin_headers, tmp_path):
        # The core data path is what a DFE is, so nothing in it waits to be enabled.
        _wire(app, tmp_path)

        entry = next(
            e
            for e in client.get("/api/v1/apps?profile=slim", headers=admin_headers).json()
            if e["service"] == "dfe-loader"
        )

        assert entry["optional"] is False
        assert entry["default_in"] is None
        assert entry["offered"] is True

    def test_an_optional_app_reports_where_it_may_be_deployed_and_that_nothing_deploys_it(
        self, client, app, admin_headers, tmp_path, optional_archiver
    ):
        _wire(app, tmp_path)

        entry = next(
            e
            for e in client.get("/api/v1/apps", headers=admin_headers).json()
            if e["service"] == "dfe-archiver"
        )

        assert entry["optional"] is True
        assert entry["profiles"] == ["scale", "scale-mesh"]
        assert entry["default_in"] == []

    def test_the_offer_is_judged_against_the_profile_the_caller_names(
        self, client, app, admin_headers, tmp_path, optional_archiver
    ):
        # The rule lives with the manifest, so the console renders an answer rather
        # than re-deriving one from the profile list.
        _wire(app, tmp_path)

        def offered(query: str) -> bool:
            listed = client.get(f"/api/v1/apps{query}", headers=admin_headers).json()
            return next(e for e in listed if e["service"] == "dfe-archiver")["offered"]

        assert offered("?profile=scale")
        assert not offered("?profile=slim")
        assert offered("")

    def test_the_instance_summary_carries_them_too(
        self, client, app, admin_headers, tmp_path, optional_archiver
    ):
        _wire(app, tmp_path)
        client.post(
            "/api/v1/apps/dfe-archiver/instances",
            json={"instance": "default"},
            headers=admin_headers,
        )

        got = client.get("/api/v1/apps/dfe-archiver/default", headers=admin_headers).json()

        assert got["optional"] is True
        assert got["profiles"] == ["scale", "scale-mesh"]
        assert got["default_in"] == []


class TestLifecycle:
    def test_deploy_creates_the_overlay_and_lists_the_instance(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        resp = _deploy(client, admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True
        assert resp.json()["commit_sha"]

        doc = gc.get("helmvars", f"{VRL}-edge-values")
        assert doc["deploy"] == {"service": VRL, "instance": "edge"}
        # The chart's own top-level dial. `env` there is a string (the deployment
        # environment) feeding labels and the namespace, so it must stay untouched.
        assert doc["otelServiceName"] == f"{VRL}-edge"
        assert "env" not in doc

        listed = client.get("/api/v1/apps", headers=admin_headers)
        by_service = {e["service"]: e for e in listed.json()}
        assert by_service[VRL]["instances"] == ["edge"]

    def test_deploying_twice_conflicts(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _deploy(client, admin_headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "already_exists"

    def test_unknown_app_is_404(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.post(
            "/api/v1/apps/dfe-nonesuch/instances",
            json={"instance": "edge"},
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "unknown_app"

    def test_invalid_instance_name_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = _deploy(client, admin_headers, instance="Not A Label")
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_instance"

    def test_a_source_bound_instance_needs_its_source(self, client, app, admin_headers, tmp_path):
        """The instance IS the source, so a name no source answers to is refused.

        Without this the deploy succeeds and the transform consumes a landing
        topic nothing ever writes to.
        """
        _wire(app, tmp_path)
        resp = client.post(
            f"/api/v1/apps/{VRL}/instances",
            json={"instance": "nosuchsource"},
            headers=admin_headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "unknown_source"
        assert "nosuchsource" in resp.json()["message"]

    def test_a_defined_source_deploys(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        assert _live_source(client, admin_headers, "realsource", {"engine": "vrl"})
        resp = client.post(
            f"/api/v1/apps/{VRL}/instances",
            json={"instance": "realsource"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text

    def test_an_app_that_is_not_source_bound_needs_no_source(
        self, client, app, admin_headers, tmp_path
    ):
        """The guard is per-app: a pool has no source binding to check."""
        _wire(app, tmp_path)
        resp = client.post(
            "/api/v1/apps/dfe-receiver/instances",
            json={"instance": "default"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text

    def test_undeploy_removes_the_overlay(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = client.delete(BASE, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert client.get(f"{BASE}/values", headers=admin_headers).status_code == 404

    def test_undeploying_what_is_not_deployed_is_404(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.delete(BASE, headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_deployed"

    def test_viewer_cannot_deploy(self, client, app, viewer_headers, tmp_path):
        _wire(app, tmp_path)
        assert _deploy(client, viewer_headers).status_code == 403


class TestCommitPolicy:
    def test_controller_owned_replica_count_is_refused_on_create(
        self, client, app, admin_headers, tmp_path
    ):
        # The whole-document write path must apply the same per-path commit policy
        # the single-var helm route applies, or it becomes a way around it.
        _wire(app, tmp_path)
        resp = _deploy(client, admin_headers, values={"replicaCount": 5})
        assert resp.status_code == 403
        assert resp.json()["code"] == "policy_violation"

    def test_floating_image_tag_is_refused_on_create(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = _deploy(client, admin_headers, values={"image.tag": "latest"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "policy_violation"


class TestScaling:
    def test_read_and_write_the_dials(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)

        resp = client.put(
            f"{BASE}/scaling",
            json={"min_replicas": 2, "max_replicas": 20, "cpu_request": "500m"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True

        got = client.get(f"{BASE}/scaling", headers=admin_headers).json()
        assert got["supported"] is True
        assert got["min_replicas"] == 2
        assert got["max_replicas"] == 20
        assert got["cpu_request"] == "500m"

    def test_max_below_min_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = client.put(
            f"{BASE}/scaling", json={"min_replicas": 5, "max_replicas": 2}, headers=admin_headers
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_dial"

    def test_dials_are_refused_off_kubernetes(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path, target="docker")
        _deploy(client, admin_headers)
        resp = client.put(f"{BASE}/scaling", json={"max_replicas": 4}, headers=admin_headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "scaling_unsupported"

    def test_reading_dials_off_kubernetes_explains_rather_than_hides(
        self, client, app, admin_headers, tmp_path
    ):
        # The dials must render disabled with a reason, not vanish from the UI.
        _wire(app, tmp_path, target="docker")
        _deploy(client, admin_headers)
        got = client.get(f"{BASE}/scaling", headers=admin_headers).json()
        assert got["supported"] is False
        assert got["deploy_target"] == "docker"
        assert "docker" in got["reason"]

    def test_unknown_target_refuses_by_default(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path, target="unknown")
        _deploy(client, admin_headers)
        got = client.get(f"{BASE}/scaling", headers=admin_headers).json()
        assert got["supported"] is False

    def test_replica_count_round_trips_with_keda_off(self, client, app, admin_headers, tmp_path):
        # Without this dial a deployment with KEDA disabled has no settable count.
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = client.put(
            f"{BASE}/scaling",
            json={"keda_enabled": False, "replica_count": 3},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        got = client.get(f"{BASE}/scaling", headers=admin_headers).json()
        assert got["keda_enabled"] is False
        assert got["replica_count"] == 3

    def test_replica_count_while_keda_is_on_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        client.put(f"{BASE}/scaling", json={"keda_enabled": True}, headers=admin_headers)
        resp = client.put(f"{BASE}/scaling", json={"replica_count": 3}, headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_dial"
        assert "KEDA is enabled" in resp.json()["message"]

    def test_a_negative_replica_count_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = client.put(f"{BASE}/scaling", json={"replica_count": -1}, headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_dial"


class TestFiles:
    def test_write_read_list_delete(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        path = f"{BASE}/files/transforms/000_parse.vrl"

        resp = client.put(path, json={"content": VRL_SOURCE}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True
        # dfe-transform-vrl compiles VRL at startup, so a write needs a pod roll.
        assert resp.json()["reload"] == "roll"

        got = client.get(path, headers=admin_headers).json()
        assert got["content"] == VRL_SOURCE
        assert got["language"] == "vrl"

        listed = client.get(f"{BASE}/files/transforms", headers=admin_headers).json()
        assert [f["name"] for f in listed] == ["000_parse.vrl"]

        assert client.delete(path, headers=admin_headers).status_code == 200
        assert client.get(f"{BASE}/files/transforms", headers=admin_headers).json() == []

    def test_rewriting_identical_content_is_not_a_change(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        path = f"{BASE}/files/transforms/000_parse.vrl"
        client.put(path, json={"content": VRL_SOURCE}, headers=admin_headers)
        resp = client.put(path, json={"content": VRL_SOURCE}, headers=admin_headers)
        assert resp.json()["changed"] is False

    def test_wrong_extension_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = client.put(
            f"{BASE}/files/transforms/notes.txt", json={"content": "x"}, headers=admin_headers
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_filename"

    def test_missing_file_is_404(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = client.get(f"{BASE}/files/transforms/nope.vrl", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_such_file"

    def test_app_without_a_file_set_is_404(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        client.post(
            "/api/v1/apps/dfe-transform-elastic/instances",
            json={"instance": "edge"},
            headers=admin_headers,
        )
        resp = client.get(
            "/api/v1/apps/dfe-transform-elastic/edge/files/transforms", headers=admin_headers
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "unknown_file_set"

    def test_vector_reports_a_hot_reload(self, client, app, admin_headers, tmp_path):
        # The supervisor watches the transform files and SIGHUPs Vector when only
        # those changed, so a content edit takes effect without a pod roll.
        _wire(app, tmp_path)
        _live_source(client, admin_headers, "edge", {"engine": "vector"})
        client.post(
            "/api/v1/apps/dfe-transform-vector/instances",
            json={"instance": "edge"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/apps/dfe-transform-vector/edge/files/transforms/enrich.yaml",
            json={"content": "type: remap\nsource: |\n  .a = 1\n"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["reload"] == "hot"

    def test_viewer_cannot_write_a_file(self, client, app, admin_headers, viewer_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = client.put(
            f"{BASE}/files/transforms/000_parse.vrl",
            json={"content": VRL_SOURCE},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestAdversarialRegressions:
    """Router-level cover for the defects an adversarial review turned up."""

    @pytest.mark.parametrize("bad", ["\x00", "\x0c"])
    def test_control_characters_in_content_are_400(self, client, app, admin_headers, tmp_path, bad):
        # A control character produces an overlay the emitter writes and the parser
        # then refuses, so the instance is left unreadable and unrepairable.
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = client.put(
            f"{BASE}/files/transforms/000_parse.vrl",
            json={"content": f".a = 1{bad}\n"},
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "invalid_content"

    @pytest.mark.parametrize(
        "values",
        [
            {"image": {"tag": "latest"}},
            {"replicaCount": 5},
            {"keda": {"enabled": True}, "image": {"tag": "latest"}},
        ],
        ids=["nested-image-tag", "nested-replica-count", "nested-among-legal-values"],
    )
    def test_nested_values_cannot_walk_past_the_commit_policy(
        self, client, app, admin_headers, tmp_path, values
    ):
        # The policy runs over the FLATTENED document: checking only the request keys
        # lets a caller nest the offending leaf one level down and out of sight.
        _wire(app, tmp_path)
        resp = _deploy(client, admin_headers, values=values)
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "policy_violation"

    def test_a_long_instance_name_deploys_and_undeploys(self, client, app, admin_headers, tmp_path):
        # A service plus a legal 40-character instance overruns the 50-character
        # commit subject on its own, so the scope has to be trimmed before it does.
        _wire(app, tmp_path)
        service = "dfe-transform-vector"
        instance = "customer-alpha-primary"
        _live_source(client, admin_headers, instance, {"engine": "vector"})
        created = client.post(
            f"/api/v1/apps/{service}/instances",
            json={"instance": instance, "values": {}},
            headers=admin_headers,
        )
        assert created.status_code == 200, created.text
        assert created.json()["commit_sha"]

        # Undeploy carried a tighter budget than create, so it failed on names that
        # created cleanly - leaving an instance that could not be removed.
        removed = client.delete(f"/api/v1/apps/{service}/{instance}", headers=admin_headers)
        assert removed.status_code == 200, removed.text
        assert (
            client.get(
                f"/api/v1/apps/{service}/{instance}/values", headers=admin_headers
            ).status_code
            == 404
        )

    def test_the_longest_legal_instance_name_deploys(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        service = "dfe-transform-vector"
        # The source-name cap is this same 40, so the longest legal instance is
        # still a nameable source.
        instance = "c" * 40
        _live_source(client, admin_headers, instance, {"engine": "vector"})
        created = client.post(
            f"/api/v1/apps/{service}/instances",
            json={"instance": instance, "values": {}},
            headers=admin_headers,
        )
        assert created.status_code == 200, created.text
        assert (
            client.delete(f"/api/v1/apps/{service}/{instance}", headers=admin_headers).status_code
            == 200
        )

    def test_a_scale_pool_app_refuses_a_second_instance(self, client, app, admin_headers, tmp_path):
        # dfe-loader's chart names its objects from the component alone, so a second
        # config renders the same names and the two Applications fight under self-heal.
        _wire(app, tmp_path)
        first = client.post(
            "/api/v1/apps/dfe-loader/instances",
            json={"instance": "default"},
            headers=admin_headers,
        )
        assert first.status_code == 200, first.text
        second = client.post(
            "/api/v1/apps/dfe-loader/instances",
            json={"instance": "spare"},
            headers=admin_headers,
        )
        assert second.status_code == 409
        assert second.json()["code"] == "single_instance_app"

    def test_a_per_config_app_accepts_a_second_instance(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        for instance in ("alpha", "beta"):
            assert _deploy(client, admin_headers, instance).status_code == 200

        # The component is the only thing keeping the two deployments' Kubernetes
        # object names apart, because dfe-common.fullname carries no instance.
        alpha = gc.get("helmvars", f"{VRL}-alpha-values")
        beta = gc.get("helmvars", f"{VRL}-beta-values")
        assert alpha["component"] == "transform-vrl-alpha"
        assert beta["component"] == "transform-vrl-beta"

        listed = client.get("/api/v1/apps", headers=admin_headers).json()
        by_service = {e["service"]: e for e in listed}
        assert by_service[VRL]["instances"] == ["alpha", "beta"]

    def test_a_fetcher_instance_carries_its_deployed_source_stanza(
        self, client, app, admin_headers, tmp_path
    ):
        from dfe_engine.api.deps import _registries

        gc = _wire(app, tmp_path)
        assert _define_fetcher_source(client, admin_headers, "alpha").status_code == 201
        _registries["source"].set_deployed_version("alpha", "1.0.0")

        resp = client.post(
            "/api/v1/apps/dfe-fetcher/instances",
            json={"instance": "alpha"},
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        alpha = gc.get("helmvars", "dfe-fetcher-alpha-values")
        assert alpha["component"] == "fetcher-alpha"
        assert alpha["config"]["instance_id"] == "alpha"
        assert alpha["config"]["sources"]["crates_io"] == {
            "enabled": True,
            "topic": "alpha",
            "crates": ["dfe-fetcher"],
        }
        listed = client.get("/api/v1/apps", headers=admin_headers).json()
        by_service = {e["service"]: e for e in listed}
        assert by_service["dfe-fetcher"]["instances"] == ["alpha"]
        assert by_service["dfe-fetcher"]["routing_scope"] == "instance"
        assert "crates_io" in by_service["dfe-fetcher"]["source_types"]

    def test_a_fetcher_instance_for_an_undeployed_source_is_refused(
        self, client, app, admin_headers, tmp_path
    ):
        # The reconcile would remove it on the next source write anyway.
        _wire(app, tmp_path)
        assert _define_fetcher_source(client, admin_headers, "alpha").status_code == 201
        resp = client.post(
            "/api/v1/apps/dfe-fetcher/instances",
            json={"instance": "alpha"},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "source_not_live"

    def test_a_fetcher_instance_for_a_receiver_source_is_refused(
        self, client, app, admin_headers, tmp_path
    ):
        from dfe_engine.api.deps import _registries

        _wire(app, tmp_path)
        _define_source(client, admin_headers, "pushed")
        _registries["source"].set_deployed_version("pushed", "1.0.0")
        resp = client.post(
            "/api/v1/apps/dfe-fetcher/instances",
            json={"instance": "pushed"},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "routing_not_applicable"

    def test_a_scale_pool_overlay_leaves_the_component_alone(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        client.post(
            "/api/v1/apps/dfe-loader/instances",
            json={"instance": "default"},
            headers=admin_headers,
        )
        assert "component" not in gc.get("helmvars", "dfe-loader-default-values")


class TestNotDeployed:
    @pytest.mark.parametrize("suffix", ["/values", "/scaling", "/files/transforms"])
    def test_reads_against_an_undeployed_instance_are_404(
        self, client, app, admin_headers, tmp_path, suffix
    ):
        _wire(app, tmp_path)
        resp = client.get(f"{BASE}{suffix}", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_deployed"


class TestRouting:
    """Source-derived routing reaching the overlay Argo actually applies."""

    RECEIVER = "/api/v1/apps/dfe-receiver/default"

    def _deployed(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        client.post(
            "/api/v1/apps/dfe-receiver/instances",
            json={"instance": "default"},
            headers=admin_headers,
        )
        return gc

    def test_a_fresh_overlay_reports_absent_routing(self, client, app, admin_headers, tmp_path):
        # The regression this guards: a receiver running on built-in defaults
        # while every source rule ever defined is ignored.
        self._deployed(client, app, admin_headers, tmp_path)
        resp = client.get(f"{self.RECEIVER}/routing", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["absent"] is True
        assert body["drift"] is True
        assert body["compiler"] == "receiver"
        assert body["values_paths"] == {
            "routing": "config.routing",
            "destinations": "config.destinations",
        }

    def test_sync_writes_the_block_and_commits(self, client, app, admin_headers, tmp_path):
        gc = self._deployed(client, app, admin_headers, tmp_path)
        resp = client.post(f"{self.RECEIVER}/routing/sync", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True
        assert resp.json()["commit_sha"]

        doc = gc.get("helmvars", "dfe-receiver-default-values")
        assert "source_rules" in doc["config"]["routing"]
        # Both blocks land: the rules that label a record and the set that says
        # where a matched one goes.
        assert doc["config"]["destinations"]["default"] == "kafka"

    def test_after_sync_there_is_no_drift(self, client, app, admin_headers, tmp_path):
        self._deployed(client, app, admin_headers, tmp_path)
        client.post(f"{self.RECEIVER}/routing/sync", headers=admin_headers)
        body = client.get(f"{self.RECEIVER}/routing", headers=admin_headers).json()
        assert (body["drift"], body["absent"]) == (False, False)

    def test_syncing_twice_is_not_a_second_commit(self, client, app, admin_headers, tmp_path):
        self._deployed(client, app, admin_headers, tmp_path)
        client.post(f"{self.RECEIVER}/routing/sync", headers=admin_headers)
        second = client.post(f"{self.RECEIVER}/routing/sync", headers=admin_headers)
        assert second.json()["changed"] is False

    def test_an_app_without_derived_routing_is_400(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        client.post(
            "/api/v1/apps/dfe-archiver/instances",
            json={"instance": "default"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/apps/dfe-archiver/default/routing", headers=admin_headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == "routing_not_compiled"

    def test_a_viewer_cannot_sync(self, client, app, admin_headers, viewer_headers, tmp_path):
        self._deployed(client, app, admin_headers, tmp_path)
        resp = client.post(f"{self.RECEIVER}/routing/sync", headers=viewer_headers)
        assert resp.status_code == 403


class TestOptimisticConcurrency:
    """The etag a read hands out, and the If-Match write it makes possible.

    The etag is the deploy repo's HEAD, which is what the gitcrud guard compares
    against. An instance's own newest commit would go stale as soon as anything
    else in the repo was written and refuse a caller who raced nobody.
    """

    STALE = "0" * 40

    def _conflicted(self, resp):
        """Assert the shaped 409, and hand back the revision to retry against."""
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["code"] == "conflict"
        # head is what the caller re-reads against, so a conflict is recoverable
        # without a second round trip.
        return body["context"]["head"]

    def test_every_read_that_backs_a_write_carries_the_head(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _deploy(client, admin_headers)
        client.put(
            f"{BASE}/files/transforms/000_parse.vrl",
            json={"content": VRL_SOURCE},
            headers=admin_headers,
        )
        head = gc.head_revision()
        assert head

        for path in (
            f"{BASE}/values",
            f"{BASE}/scaling",
            f"{BASE}/files/transforms/000_parse.vrl",
        ):
            got = client.get(path, headers=admin_headers)
            assert got.status_code == 200, got.text
            assert got.json()["etag"] == head, path

    def test_the_routing_read_carries_the_head(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        client.post(
            "/api/v1/apps/dfe-receiver/instances",
            json={"instance": "default"},
            headers=admin_headers,
        )
        got = client.get("/api/v1/apps/dfe-receiver/default/routing", headers=admin_headers)
        assert got.status_code == 200, got.text
        assert got.json()["etag"] == gc.head_revision()

    def test_the_overlay_is_nested_under_values(self, client, app, admin_headers, tmp_path):
        # The document's keys are the chart's, so the revision cannot sit beside
        # them without risking a collision with a chart value called etag.
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        body = client.get(f"{BASE}/values", headers=admin_headers).json()
        assert body["values"]["deploy"] == {"service": VRL, "instance": "edge"}

    def test_the_etag_moves_with_every_commit(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        first = client.get(f"{BASE}/scaling", headers=admin_headers).json()["etag"]
        client.put(f"{BASE}/scaling", json={"min_replicas": 3}, headers=admin_headers)
        second = client.get(f"{BASE}/scaling", headers=admin_headers).json()["etag"]
        assert first != second

    def test_a_read_etag_writes_and_the_reused_one_then_conflicts(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        etag = client.get(f"{BASE}/scaling", headers=admin_headers).json()["etag"]

        fresh = client.put(
            f"{BASE}/scaling",
            json={"min_replicas": 2},
            headers={**admin_headers, "If-Match": etag},
        )
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["changed"] is True

        # The same etag a second time is exactly the stale-tab case.
        stale = client.put(
            f"{BASE}/scaling",
            json={"min_replicas": 4},
            headers={**admin_headers, "If-Match": etag},
        )
        head = self._conflicted(stale)
        assert head == fresh.json()["commit_sha"]

    def test_copy_refuses_a_stale_write(self, client, app, admin_headers, tmp_path):
        # A whole-file-set rewrite, so last-write-wins here costs authored content.
        _wire(app, tmp_path)
        _deploy(client, admin_headers, instance="edge")
        _deploy(client, admin_headers, instance="other")
        client.put(
            f"{BASE}/files/transforms/000_parse.vrl",
            json={"content": VRL_SOURCE},
            headers=admin_headers,
        )
        path = f"{BASE}/files/transforms/copy"

        stale = client.post(
            path,
            json={"target_instance": "other"},
            headers={**admin_headers, "If-Match": self.STALE},
        )
        head = self._conflicted(stale)

        fresh = client.post(
            path,
            json={"target_instance": "other"},
            headers={**admin_headers, "If-Match": head},
        )
        assert fresh.status_code == 200, fresh.text
        assert fresh.json()["copied"] == ["000_parse.vrl"]

    def test_relink_refuses_a_stale_write(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        created = client.post(
            "/api/v1/library",
            json={"name": "parse-syslog", "kind": "vrl", "content": VRL_SOURCE},
            headers=admin_headers,
        )
        assert created.status_code == 200, created.text
        linked = client.post(
            f"{BASE}/files/transforms/link",
            json={"name": "000_parse.vrl", "artifact": "parse-syslog"},
            headers=admin_headers,
        )
        assert linked.status_code == 200, linked.text
        # The link has to be behind before a relink writes anything: a relink that
        # moves nothing never reaches the commit, so it has no revision to guard.
        published = client.post(
            "/api/v1/library/parse-syslog/versions",
            json={"content": VRL_SOURCE + "\n.extra = true\n"},
            headers=admin_headers,
        )
        assert published.status_code == 200, published.text

        path = f"{BASE}/files/transforms/relink"
        stale = client.post(path, headers={**admin_headers, "If-Match": self.STALE})
        head = self._conflicted(stale)

        fresh = client.post(path, headers={**admin_headers, "If-Match": head})
        assert fresh.status_code == 200, fresh.text
        assert [link["name"] for link in fresh.json()["relinked"]] == ["000_parse.vrl"]

    def test_deleting_a_file_refuses_a_stale_write(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        path = f"{BASE}/files/transforms/000_parse.vrl"
        client.put(path, json={"content": VRL_SOURCE}, headers=admin_headers)

        stale = client.delete(path, headers={**admin_headers, "If-Match": self.STALE})
        head = self._conflicted(stale)

        fresh = client.delete(path, headers={**admin_headers, "If-Match": head})
        assert fresh.status_code == 200, fresh.text

    def test_deploying_refuses_a_stale_write(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        # Something has to be committed first: an empty deploy repo has no
        # revision, so there is nothing a base revision can be stale against.
        _deploy(client, admin_headers, instance="other")
        _live_source(client, admin_headers, "edge", {"engine": "vrl"})

        resp = client.post(
            f"/api/v1/apps/{VRL}/instances",
            json={"instance": "edge"},
            headers={**admin_headers, "If-Match": self.STALE},
        )
        head = self._conflicted(resp)

        fresh = client.post(
            f"/api/v1/apps/{VRL}/instances",
            json={"instance": "edge"},
            headers={**admin_headers, "If-Match": head},
        )
        assert fresh.status_code == 200, fresh.text

    def test_undeploying_refuses_a_stale_write(self, client, app, admin_headers, tmp_path):
        # A delete takes the guard too: removing an overlay somebody edited since
        # you read it destroys their change as thoroughly as overwriting it.
        _wire(app, tmp_path)
        _deploy(client, admin_headers)

        stale = client.delete(BASE, headers={**admin_headers, "If-Match": self.STALE})
        head = self._conflicted(stale)
        assert client.get(f"{BASE}/values", headers=admin_headers).status_code == 200

        fresh = client.delete(BASE, headers={**admin_headers, "If-Match": head})
        assert fresh.status_code == 200, fresh.text
        assert client.get(f"{BASE}/values", headers=admin_headers).status_code == 404

    def test_a_write_without_if_match_still_lands(self, client, app, admin_headers, tmp_path):
        # The header is opt-in: a caller that does not send one is unguarded, not
        # refused, so every existing client keeps working.
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = client.put(f"{BASE}/scaling", json={"min_replicas": 2}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True


class TestDryRun:
    """Running an authored file over sampled events, and the gates on doing it."""

    @staticmethod
    def _sampler(app, lines: list[str] | None = None):
        """A stand-in sampler returning fixed lines, so no backing service is needed."""

        class _Result:
            def __init__(self, lines):
                self.lines = lines

        class _Sampler:
            def resolve_or_raise(self, req, registry):
                return None

            async def run(self, req, ch, registry):
                return _Result(lines if lines is not None else ['{"message": "hi"}'])

        app.state.sampler = _Sampler()

    def _deployed(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        _deploy(client, admin_headers)
        client.put(
            f"{BASE}/files/transforms/000.vrl",
            json={"content": VRL_SOURCE},
            headers=admin_headers,
        )
        return gc

    def test_disabled_by_default_and_says_so(self, client, app, admin_headers, tmp_path):
        self._deployed(client, app, admin_headers, tmp_path)
        self._sampler(app)
        resp = client.post(
            f"{BASE}/files/transforms/dry-run",
            json={"name": "000.vrl"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "disabled"
        assert body["events"] == []

    def test_an_absent_backend_reports_unavailable_never_a_pass(
        self, client, app, admin_headers, tmp_path
    ):
        self._deployed(client, app, admin_headers, tmp_path)
        self._sampler(app)
        app.state.settings.transform_validation.dry_run = True
        resp = client.post(
            f"{BASE}/files/transforms/dry-run",
            json={"name": "000.vrl"},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "unavailable"

    def test_it_samples_the_instances_own_source_by_default(
        self, client, app, admin_headers, tmp_path
    ):
        self._deployed(client, app, admin_headers, tmp_path)
        self._sampler(app, ['{"a": 1}', '{"a": 2}'])
        resp = client.post(
            f"{BASE}/files/transforms/dry-run",
            json={"name": "000.vrl"},
            headers=admin_headers,
        )
        body = resp.json()
        assert body["source"] == "edge"
        assert body["sampled"] == 2

    def test_unsaved_content_runs_without_being_committed(
        self, client, app, admin_headers, tmp_path
    ):
        gc = self._deployed(client, app, admin_headers, tmp_path)
        self._sampler(app)
        before = gc.get("helmvars", f"{VRL}-edge-values")
        client.post(
            f"{BASE}/files/transforms/dry-run",
            json={"name": "000.vrl", "content": ".completely = different\n"},
            headers=admin_headers,
        )
        assert gc.get("helmvars", f"{VRL}-edge-values") == before

    def test_a_missing_file_is_404(self, client, app, admin_headers, tmp_path):
        self._deployed(client, app, admin_headers, tmp_path)
        self._sampler(app)
        resp = client.post(
            f"{BASE}/files/transforms/dry-run",
            json={"name": "nope.vrl"},
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_such_file"

    def test_the_event_ceiling_is_enforced_at_the_edge(self, client, app, admin_headers, tmp_path):
        self._deployed(client, app, admin_headers, tmp_path)
        self._sampler(app)
        resp = client.post(
            f"{BASE}/files/transforms/dry-run",
            json={"name": "000.vrl", "limit": 5000},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_a_viewer_holding_sampler_read_still_cannot_execute(
        self, client, app, admin_headers, viewer_headers, tmp_path
    ):
        # data_viewer has sampler:read but not dryrun:execute - reading source data
        # is not the same privilege as running code over it.
        self._deployed(client, app, admin_headers, tmp_path)
        self._sampler(app)
        resp = client.post(
            f"{BASE}/files/transforms/dry-run",
            json={"name": "000.vrl"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_an_operator_holding_both_grants_may_execute(
        self, client, app, admin_headers, operator_headers, tmp_path
    ):
        # A dry run needs dryrun:execute (infra_admin) AND sampler:read
        # (data_analyst); the operator resolves both, so both gates pass.
        self._deployed(client, app, admin_headers, tmp_path)
        self._sampler(app)
        resp = client.post(
            f"{BASE}/files/transforms/dry-run",
            json={"name": "000.vrl"},
            headers=operator_headers,
        )
        assert resp.status_code == 200, resp.text
