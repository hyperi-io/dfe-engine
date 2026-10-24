#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_ingest_rules_api.py
#  Purpose:      The ingest rules API: block order, profile bindings and rule refusals
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The rule order and the deployment's profile bindings, over HTTP, against a real deploy repo.

A reorder or a binding change is a rules-only change, so the receiver takes it
where it stands. These tests run on a Compose deployment, where the engine renders
the receiver's config itself and judges each change against the paths the
receiver binds at startup, so ``reload: hot`` is that judgement, not a constant.
"""

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore
from dfe_engine.ingest.models import RuleOrder
from dfe_engine.ingest.store import IngestStore, order_etag
from dfe_engine.source.registry import SourceRegistry

RECEIVER_OVERLAY = "dfe-receiver-main-values"
LEGACY = "dfe-legacy-2x"
ORDER = "/api/v1/ingest/rules/order"


def _wire(app, tmp_path) -> GitCrud:
    """A Compose deployment with a deploy repo, a deployed receiver and the shipped profiles."""
    from dfe_engine.api.deps import _registries
    from dfe_engine.appmgmt import appconfig

    app.state.settings.env = "dev"
    app.state.settings.deployment.target = "docker"
    app.state.settings.deployment.app_config_dir = str(tmp_path / "app-config")
    gc = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    gc.put(
        "helmvars",
        RECEIVER_OVERLAY,
        {"deploy": {"service": "dfe-receiver", "instance": "main"}},
        "test",
        message="test: deploy receiver",
    )
    IngestStore(gc).seed_core_profiles()
    _registries["source"] = SourceRegistry(crud=gc)
    appconfig.render_and_report(gc, app.state.settings)
    return gc


def _create(client, headers, name: str, **body) -> None:
    resp = client.post("/api/v1/sources", json={"source": name, **body}, headers=headers)
    assert resp.status_code == 201, resp.text


def _dataset(value: str) -> dict:
    return {"field": "data_stream.dataset", "value": value}


def _overlay_rule_ids(gc: GitCrud) -> list[str]:
    doc = gc.get("helmvars", RECEIVER_OVERLAY)
    return [row["id"] for row in doc["config"]["rules"]["rules"]]


@pytest.fixture
def wired(client, app, admin_headers, tmp_path) -> GitCrud:
    gc = _wire(app, tmp_path)
    _create(client, admin_headers, "cisco-asa", match=_dataset("cisco_asa.log"))
    _create(
        client,
        admin_headers,
        "cisco-ios",
        match={"any": [_dataset("cisco_ios.log"), _dataset("cisco_ios.debug")]},
    )
    return gc


def _listed(client, headers) -> tuple[list[dict], str]:
    resp = client.get("/api/v1/ingest/rules", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"], resp.headers["ETag"]


class TestListingTheBlocks:
    def test_every_block_is_listed_in_the_order_it_runs_with_the_default_pinned(
        self, client, admin_headers, wired
    ):
        items, etag = _listed(client, admin_headers)

        assert [(item["target"], item["id"], item["position"]) for item in items] == [
            ("_source", "source.cisco-asa", 1),
            ("_source", "source.cisco-ios", 2),
            ("_source", "default", 3),
            ("_org_id", "default", 1),
        ]
        assert [item["pinned"] for item in items] == [False, False, True, True]
        assert [rule["id"] for rule in items[1]["rules"]] == [
            "source.cisco-ios.1",
            "source.cisco-ios.2",
        ]
        assert etag == order_etag(RuleOrder())

    def test_a_viewer_reads_and_cannot_reorder(self, client, viewer_headers, wired):
        assert client.get("/api/v1/ingest/rules", headers=viewer_headers).status_code == 200
        resp = client.post(
            ORDER,
            json={"target": "_source", "blocks": ["source.cisco-ios", "source.cisco-asa"]},
            headers={**viewer_headers, "If-Match": order_etag(RuleOrder())},
        )
        assert resp.status_code == 403

    def test_without_a_deploy_repo_the_rules_are_not_configured(self, client, admin_headers):
        assert client.get("/api/v1/ingest/rules", headers=admin_headers).status_code == 503

    def test_a_cross_origin_console_can_read_the_etag(self, api_settings):
        origin = "https://console.example"
        with TestClient(create_app(settings=api_settings, cors_origins=[origin])) as c:
            resp = c.get("/api/v1/ingest/rules", headers={"Origin": origin})

        assert resp.headers["access-control-allow-origin"] == origin
        assert "etag" in resp.headers["access-control-expose-headers"].lower()


class TestReordering:
    BODY = {"target": "_source", "blocks": ["source.cisco-ios", "source.cisco-asa"]}

    def test_a_reorder_lands_in_the_receiver_rules_and_answers_hot(
        self, client, admin_headers, wired
    ):
        _, etag = _listed(client, admin_headers)

        resp = client.post(ORDER, json=self.BODY, headers={**admin_headers, "If-Match": etag})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["reload"] == "hot"
        assert body["restart_required"] == []
        assert body["apps_synced"] == ["dfe-receiver/main: sync routing"]
        assert [block["id"] for block in body["blocks"]] == [
            "source.cisco-ios",
            "source.cisco-asa",
            "default",
        ]
        assert _overlay_rule_ids(wired) == [
            "source.cisco-ios.1",
            "source.cisco-ios.2",
            "source.cisco-asa",
        ]
        assert resp.headers["ETag"] == _listed(client, admin_headers)[1]

    def test_without_if_match_it_is_428(self, client, admin_headers, wired):
        resp = client.post(ORDER, json=self.BODY, headers=admin_headers)
        assert resp.status_code == 428
        assert resp.json()["code"] == "precondition_required"

    def test_a_stale_if_match_is_412_and_writes_nothing(self, client, admin_headers, wired):
        _, etag = _listed(client, admin_headers)
        client.post(ORDER, json=self.BODY, headers={**admin_headers, "If-Match": etag})
        head = wired.head_revision()

        resp = client.post(
            ORDER,
            json={**self.BODY, "blocks": ["source.cisco-asa", "source.cisco-ios"]},
            headers={**admin_headers, "If-Match": etag},
        )

        assert resp.status_code == 412
        assert resp.json()["code"] == "etag_mismatch"
        assert resp.json()["context"]["etag"] == _listed(client, admin_headers)[1]
        assert wired.head_revision() == head

    def test_a_list_missing_a_block_is_409_with_the_current_blocks(
        self, client, admin_headers, wired
    ):
        _, etag = _listed(client, admin_headers)
        # A source created after the list was read is a block the reorder does not name.
        _create(client, admin_headers, "cisco-ftd", match=_dataset("cisco_ftd.log"))

        resp = client.post(ORDER, json=self.BODY, headers={**admin_headers, "If-Match": etag})

        assert resp.status_code == 409
        assert resp.json()["code"] == "order_conflict"
        assert resp.json()["context"] == {
            "target": "_source",
            "blocks": ["source.cisco-asa", "source.cisco-ftd", "source.cisco-ios"],
        }

    @pytest.mark.parametrize(
        ("blocks", "detail"),
        [
            (["source.cisco-ios", "source.cisco-asa", "default"], "'default' is pinned"),
            (["source.cisco-ios", "source.cisco-ios"], "'source.cisco-ios' is listed twice"),
        ],
    )
    def test_a_pinned_or_repeated_block_is_422(self, client, admin_headers, wired, blocks, detail):
        _, etag = _listed(client, admin_headers)

        resp = client.post(
            ORDER,
            json={"target": "_source", "blocks": blocks},
            headers={**admin_headers, "If-Match": etag},
        )

        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_order"
        assert any(detail in error["message"] for error in resp.json()["errors"])

    def test_a_dry_run_previews_the_order_and_commits_nothing(self, client, admin_headers, wired):
        _, etag = _listed(client, admin_headers)
        head = wired.head_revision()

        resp = client.post(
            f"{ORDER}?dry_run=true", json=self.BODY, headers={**admin_headers, "If-Match": etag}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["dry_run"] is True
        assert [block["id"] for block in resp.json()["blocks"]][:2] == self.BODY["blocks"]
        assert wired.head_revision() == head


class TestTheRefusedPushIsNotReappliedBlind:
    """A conditional write against a head that moved is re-checked against what is there now."""

    def _store(self, tmp_path) -> tuple[GitCrud, IngestStore]:
        gc = GitCrud(
            GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry()
        )
        # A deploy repo with no commit has no head for a write to be conditional on.
        gc.put("gov_settings", "seeded", {"a": 0}, "test", message="test: first commit")
        return gc, IngestStore(gc)

    def test_an_unrelated_commit_in_between_is_written_over_once_rechecked(self, tmp_path):
        from dfe_engine.api.v1.ingest import _conditional_write

        gc, store = self._store(tmp_path)
        order, head = store.order_at_head()
        gc.put("gov_settings", "unrelated", {"a": 1}, "test", message="test: another write")
        desired = order.with_ids("_source", ["source.b", "source.a"])

        _conditional_write(
            lambda base: store.put_order(desired, "test", base_revision=base),
            RuleOrder.model_validate,
            order_etag,
            order_etag(order),
            head,
            "rule order",
        )

        assert store.order() == desired

    def test_a_reorder_in_between_is_412_and_kept(self, tmp_path):
        from scalo.api import ProblemError

        from dfe_engine.api.v1.ingest import _conditional_write

        gc, store = self._store(tmp_path)
        order, head = store.order_at_head()
        theirs = order.with_ids("_source", ["source.a", "source.b"])
        store.put_order(theirs, "other", base_revision=None)
        mine = order.with_ids("_source", ["source.b", "source.a"])

        with pytest.raises(ProblemError) as caught:
            _conditional_write(
                lambda base: store.put_order(mine, "test", base_revision=base),
                RuleOrder.model_validate,
                order_etag,
                order_etag(order),
                head,
                "rule order",
            )

        assert caught.value.status == 412
        assert store.order() == theirs


class TestDeploymentBindings:
    def test_binding_a_profile_compiles_its_rows_and_answers_hot(
        self, client, admin_headers, wired
    ):
        _create(
            client,
            admin_headers,
            "auth",
            match=_dataset("system.auth"),
            aliases={LEGACY: ["authentication"]},
        )
        got = client.get("/api/v1/ingest/compat", headers=admin_headers)
        assert got.json() == {"profiles": []}

        resp = client.put(
            "/api/v1/ingest/compat",
            json={"profiles": [{"profile": LEGACY}]},
            headers={**admin_headers, "If-Match": got.headers["ETag"]},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["reload"] == "hot"
        ids = _overlay_rule_ids(wired)
        assert f"compat.{LEGACY}.category" in ids
        assert f"compat.{LEGACY}.org-tags.auth" in ids
        lookups = wired.get("helmvars", RECEIVER_OVERLAY)["config"]["rules"]["lookups"]
        assert lookups == {f"compat.{LEGACY}.aliases": {"authentication": "auth"}}

    def test_a_binding_to_a_profile_not_stored_is_422(self, client, admin_headers, wired):
        etag = client.get("/api/v1/ingest/compat", headers=admin_headers).headers["ETag"]
        resp = client.put(
            "/api/v1/ingest/compat",
            json={"profiles": [{"profile": "splunk-hec"}]},
            headers={**admin_headers, "If-Match": etag},
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_binding"
        assert resp.json()["errors"][0]["field"] == "profiles.0"

    def test_without_if_match_the_apply_is_428(self, client, admin_headers, wired):
        resp = client.put(
            "/api/v1/ingest/compat", json={"profiles": [{"profile": LEGACY}]}, headers=admin_headers
        )
        assert resp.status_code == 428


class TestProfiles:
    def test_the_shipped_profiles_are_listed(self, client, admin_headers, wired):
        resp = client.get("/api/v1/ingest/compat-profiles", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert [
            (item["id"], item["resource_type"], item["current"]) for item in resp.json()["items"]
        ] == [
            (LEGACY, "core", "1.0.0"),
            ("elastic-agent", "core", "1.0.0"),
        ]
        assert resp.json()["items"][1]["opt_in"] == ["org-namespace"]

    def test_one_profile_carries_every_version(self, client, admin_headers, wired):
        resp = client.get(f"/api/v1/ingest/compat-profiles/{LEGACY}", headers=admin_headers)
        assert resp.status_code == 200
        assert list(resp.json()["versions"]) == ["1.0.0"]
        assert resp.headers["ETag"]

    def test_a_profile_not_stored_is_404(self, client, admin_headers, wired):
        resp = client.get("/api/v1/ingest/compat-profiles/splunk-hec", headers=admin_headers)
        assert resp.status_code == 404


class TestRuleRefusals:
    def test_a_when_the_receiver_refuses_is_422_with_its_span(self, client, admin_headers, wired):
        when = 'message.matches("^a")'
        resp = client.post(
            "/api/v1/sources", json={"source": "regex", "when": when}, headers=admin_headers
        )

        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "invalid_rule"
        [error] = body["context"]["rule_errors"]
        assert error["field"] == "when"
        assert when[error["span"]["start"] : error["span"]["end"]] == "matches"
        assert body["errors"][0]["field"] == "when"
