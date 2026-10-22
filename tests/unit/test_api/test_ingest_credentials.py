#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_ingest_credentials.py
#  Purpose:      Tests for an org's ingest credentials and the reserved default org
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for /api/v1/orgs/{org}/ingest-credentials, and the default org's reservation.

A credential's token is minted once and comes back once. Its binding reaches the
receiver's overlay as tenant ids, and its ``<id>:<token>`` line reaches the bearer
secret of every listener it is scoped to.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.api.v1 import ingest_credentials as ingest_credentials_api
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore
from dfe_engine.orgs.ingest_tokens import bearer_secret_path, credential_secret_path
from dfe_engine.settings import IngestSettings, SecretsSettings
from tests.support.local_kube_api import local_kube_api, mirror_for

BASE = "/api/v1/orgs/acme/ingest-credentials"
CREATE = {"If-None-Match": "*"}
RECEIVER_OVERLAY = "dfe-receiver-main-values"


@pytest.fixture
def orgs(client, admin_headers) -> None:
    """Two tenant-fenced orgs: acme with tenant ids of its own, beta with its name."""
    for body in ({"name": "acme", "org_ids": ["acme-prod", "acme-dev"]}, {"name": "beta"}):
        resp = client.post("/api/v1/orgs", json=body, headers=admin_headers)
        assert resp.status_code == 201, resp.text


def _create(client, headers, name="agents", **body):
    return client.put(f"{BASE}/{name}", json=body, headers={**headers, **CREATE})


def _secret(app, path: str) -> str | None:
    return app.state.dfe_secrets.get_or_none(path)


def _bearer(app, listener: str) -> str:
    value = _secret(app, bearer_secret_path(listener))
    assert value is not None, f"no bearer secret for {listener}"
    return value


class TestApplyCreates:
    def test_a_create_mints_the_token_once(self, client, app, admin_headers, orgs):
        resp = _create(client, admin_headers, orgs=["acme", "beta"], listeners=["http", "otlp"])

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["token"]
        assert body["dry_run"] is False
        assert {k: body[k] for k in ("name", "org", "orgs", "default_org", "listeners")} == {
            "name": "agents",
            "org": "acme",
            "orgs": ["acme", "beta"],
            "default_org": "acme",
            "listeners": ["http", "otlp"],
        }
        assert resp.headers["ETag"]
        assert _secret(app, credential_secret_path("agents")) == body["token"]

        read = client.get(f"{BASE}/agents", headers=admin_headers)
        assert read.status_code == 200
        assert "token" not in read.json()
        assert read.headers["ETag"] == resp.headers["ETag"]

    def test_the_token_reaches_the_bearer_secret_of_each_listener_it_is_scoped_to(
        self, client, app, admin_headers, orgs
    ):
        token = _create(client, admin_headers, listeners=["http"]).json()["token"]

        assert _secret(app, bearer_secret_path("http")) == f"agents:{token}\n"
        assert token not in _bearer(app, "otlp")

    def test_unset_fields_bind_the_owning_org_on_every_listener(self, client, admin_headers, orgs):
        body = _create(client, admin_headers).json()

        assert (body["orgs"], body["default_org"]) == (["acme"], "acme")
        assert body["listeners"] == ["http", "grpc", "otlp", "splunk_hec", "prometheus_rw"]

    def test_a_create_with_no_precondition_is_428(self, client, admin_headers, orgs):
        resp = client.put(f"{BASE}/agents", json={}, headers=admin_headers)

        assert resp.status_code == 428
        assert resp.json()["code"] == "precondition_required"

    def test_an_id_another_org_holds_is_409(self, client, admin_headers, orgs):
        _create(client, admin_headers)

        resp = client.put(
            "/api/v1/orgs/beta/ingest-credentials/agents",
            json={},
            headers={**admin_headers, **CREATE},
        )

        assert resp.status_code == 409
        assert resp.json()["code"] == "credential_id_taken"

    def test_creating_one_that_exists_is_412(self, client, admin_headers, orgs):
        _create(client, admin_headers)

        assert _create(client, admin_headers).status_code == 412

    def test_an_id_the_receiver_could_not_key_is_422(self, client, admin_headers, orgs):
        resp = _create(client, admin_headers, name="-agents")

        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_credential_id"

    def test_an_org_that_does_not_exist_is_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/orgs/nobody/ingest-credentials/agents",
            json={},
            headers={**admin_headers, **CREATE},
        )

        assert resp.status_code == 404

    def test_a_dry_run_mints_and_stores_nothing(self, client, app, admin_headers, orgs):
        resp = client.put(
            f"{BASE}/agents?dry_run=true", json={}, headers={**admin_headers, **CREATE}
        )

        assert resp.status_code == 200, resp.text
        assert (resp.json()["dry_run"], resp.json()["token"]) == (True, None)
        assert client.get(f"{BASE}/agents", headers=admin_headers).status_code == 404
        assert _secret(app, credential_secret_path("agents")) is None


class TestClampRefusal:
    """A binding the receiver would refuse, or clamp every record away from, is 422."""

    def test_a_default_org_outside_the_binding_is_422(self, client, admin_headers, orgs):
        resp = _create(client, admin_headers, orgs=["acme"], default_org="beta")

        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "invalid_binding"
        assert [e["field"] for e in body["errors"]] == ["default_org"]

    def test_an_org_that_is_not_registered_is_422(self, client, admin_headers, orgs):
        resp = _create(client, admin_headers, orgs=["acme", "victim"])

        assert resp.status_code == 422
        assert [e["field"] for e in resp.json()["errors"]] == ["orgs.1"]

    def test_a_binding_without_the_owning_org_is_422(self, client, admin_headers, orgs):
        resp = _create(client, admin_headers, orgs=["beta"], default_org="beta")

        assert resp.status_code == 422
        assert [e["field"] for e in resp.json()["errors"]] == ["orgs"]

    def test_every_problem_is_named_at_once(self, client, admin_headers, orgs):
        resp = _create(
            client,
            admin_headers,
            orgs=["acme", "acme", "victim"],
            default_org="victim",
            listeners=["http", "http"],
        )

        fields = sorted(e["field"] for e in resp.json()["errors"])
        assert fields == ["listeners", "orgs.1", "orgs.2"]

    def test_a_refused_binding_mints_nothing(self, client, app, admin_headers, orgs):
        _create(client, admin_headers, orgs=["acme"], default_org="beta")

        assert _secret(app, credential_secret_path("agents")) is None


class TestUnreadableBackend:
    """Listeners on a backend the receiver reads no secret from are refused at create."""

    @pytest.fixture
    def ansible_vault(self, monkeypatch) -> None:
        settings = SimpleNamespace(secrets=SecretsSettings(provider="ansible_vault"))
        monkeypatch.setattr(ingest_credentials_api, "get_settings", lambda: settings)

    def test_listeners_on_ansible_vault_are_422_and_mint_nothing(
        self, client, app, admin_headers, orgs, ansible_vault
    ):
        resp = _create(client, admin_headers, listeners=["http"])

        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "invalid_binding"
        assert [e["field"] for e in body["errors"]] == ["listeners"]
        assert "DFE_SECRETS_PROVIDER=ansible_vault" in body["errors"][0]["message"]
        assert _secret(app, credential_secret_path("agents")) is None


class TestApplyUpdates:
    def test_an_update_keeps_the_token_and_changes_the_binding(
        self, client, app, admin_headers, orgs
    ):
        created = _create(client, admin_headers, listeners=["http"])
        token = created.json()["token"]

        resp = client.put(
            f"{BASE}/agents",
            json={"orgs": ["acme", "beta"], "listeners": ["otlp"]},
            headers={**admin_headers, "If-Match": created.headers["ETag"]},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["token"] is None
        assert resp.json()["orgs"] == ["acme", "beta"]
        assert resp.json()["created_at"] == created.json()["created_at"]
        assert _secret(app, credential_secret_path("agents")) == token
        assert _secret(app, bearer_secret_path("otlp")) == f"agents:{token}\n"
        assert token not in _bearer(app, "http")

    def test_a_stale_etag_is_412(self, client, admin_headers, orgs):
        _create(client, admin_headers)

        resp = client.put(f"{BASE}/agents", json={}, headers={**admin_headers, "If-Match": '"x"'})

        assert resp.status_code == 412
        assert resp.json()["code"] == "etag_mismatch"


class TestDeleteAndRotate:
    def test_a_deleted_credential_s_token_leaves_every_secret(
        self, client, app, admin_headers, orgs
    ):
        created = _create(client, admin_headers)
        token = created.json()["token"]

        resp = client.delete(
            f"{BASE}/agents", headers={**admin_headers, "If-Match": created.headers["ETag"]}
        )

        assert resp.status_code == 204
        assert client.get(f"{BASE}/agents", headers=admin_headers).status_code == 404
        assert _secret(app, credential_secret_path("agents")) is None
        assert token not in _bearer(app, "http")

    def test_a_delete_needs_if_match(self, client, admin_headers, orgs):
        _create(client, admin_headers)

        assert client.delete(f"{BASE}/agents", headers=admin_headers).status_code == 428

    def test_a_dry_run_delete_keeps_it(self, client, admin_headers, orgs):
        created = _create(client, admin_headers)

        resp = client.delete(
            f"{BASE}/agents?dry_run=true",
            headers={**admin_headers, "If-Match": created.headers["ETag"]},
        )

        assert resp.status_code == 204
        assert client.get(f"{BASE}/agents", headers=admin_headers).status_code == 200

    def test_a_rotate_replaces_the_token_and_keeps_the_binding(
        self, client, app, admin_headers, orgs
    ):
        created = _create(client, admin_headers, listeners=["http"]).json()

        resp = client.post(f"{BASE}/agents/rotate", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]
        assert token
        assert token != created["token"]
        assert resp.json()["orgs"] == created["orgs"]
        assert _secret(app, bearer_secret_path("http")) == f"agents:{token}\n"

    def test_a_dry_run_rotate_mints_nothing(self, client, app, admin_headers, orgs):
        created = _create(client, admin_headers).json()

        resp = client.post(f"{BASE}/agents/rotate?dry_run=true", headers=admin_headers)

        assert resp.json()["token"] is None
        assert _secret(app, credential_secret_path("agents")) == created["token"]


@pytest.fixture
def acme_admin(client, app, admin_headers, api_settings, orgs) -> dict[str, str]:
    """A user holding the admin role in acme alone."""
    if app.state.account_store.get("acme-admin") is None:
        app.state.account_store.create("acme-admin", "pw-acme-admin")
    resp = client.post(
        "/api/v1/auth/groups",
        json={
            "name": "acme-admins",
            "roles": ["admin"],
            "scope": "org:acme",
            "members": ["acme-admin"],
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    token = create_access_token(
        data={"sub": "acme-admin", "org_id": "default"}, settings=api_settings
    )
    return {"Authorization": f"Bearer {token}"}


class TestBindingAnotherOrg:
    """Binding an org through the body takes ``org:write`` there, as the path org does."""

    def test_an_org_admin_may_not_bind_an_org_it_does_not_hold(
        self, client, app, admin_headers, acme_admin
    ):
        resp = _create(client, acme_admin, orgs=["acme", "beta"])

        assert resp.status_code == 403, resp.text
        assert client.get(f"{BASE}/agents", headers=admin_headers).status_code == 404
        assert _secret(app, credential_secret_path("agents")) is None

    def test_an_org_admin_may_not_widen_a_binding_to_another_org(
        self, client, admin_headers, acme_admin
    ):
        created = _create(client, admin_headers)

        resp = client.put(
            f"{BASE}/agents",
            json={"orgs": ["acme", "beta"]},
            headers={**acme_admin, "If-Match": created.headers["ETag"]},
        )

        assert resp.status_code == 403, resp.text
        assert client.get(f"{BASE}/agents", headers=admin_headers).json()["orgs"] == ["acme"]

    def test_an_org_admin_may_bind_its_own_org(self, client, acme_admin):
        resp = _create(client, acme_admin)

        assert resp.status_code == 201, resp.text
        assert resp.json()["orgs"] == ["acme"]

    def test_a_system_admin_may_bind_any_org(self, client, admin_headers, orgs):
        resp = _create(client, admin_headers, orgs=["acme", "beta"])

        assert resp.status_code == 201, resp.text
        assert resp.json()["orgs"] == ["acme", "beta"]


class TestADisabledOrg:
    def test_its_tokens_stop_authenticating_until_it_is_enabled_again(
        self, client, app, admin_headers, orgs
    ):
        token = _create(client, admin_headers).json()["token"]

        disabled = client.put("/api/v1/orgs/acme", json={"enabled": False}, headers=admin_headers)

        assert disabled.status_code == 200, disabled.text
        assert token not in _bearer(app, "http")
        assert _secret(app, credential_secret_path("agents")) == token

        enabled = client.put("/api/v1/orgs/acme", json={"enabled": True}, headers=admin_headers)

        assert enabled.status_code == 200, enabled.text
        assert _bearer(app, "http") == f"agents:{token}\n"

    def test_a_credential_write_while_it_is_disabled_reaches_no_listener(
        self, client, app, admin_headers, orgs
    ):
        client.put("/api/v1/orgs/acme", json={"enabled": False}, headers=admin_headers)

        token = _create(client, admin_headers).json()["token"]

        assert token not in _bearer(app, "http")


class TestReads:
    def test_the_list_is_the_org_s_own_and_never_a_token(self, client, admin_headers, orgs):
        _create(client, admin_headers)
        client.put(
            "/api/v1/orgs/beta/ingest-credentials/beta-hec",
            json={},
            headers={**admin_headers, **CREATE},
        )

        page = client.get(BASE, headers=admin_headers).json()

        assert [c["name"] for c in page["items"]] == ["agents"]
        assert page["next_page_token"] is None
        assert "token" not in page["items"][0]

    def test_another_org_s_credential_is_not_found_here(self, client, admin_headers, orgs):
        client.put(
            "/api/v1/orgs/beta/ingest-credentials/beta-hec",
            json={},
            headers={**admin_headers, **CREATE},
        )

        assert client.get(f"{BASE}/beta-hec", headers=admin_headers).status_code == 404

    def test_the_list_pages(self, client, admin_headers, orgs):
        for name in ("a1", "a2", "a3"):
            _create(client, admin_headers, name=name)

        first = client.get(f"{BASE}?page_size=2", headers=admin_headers).json()
        rest = client.get(
            f"{BASE}?page_size=2&page_token={first['next_page_token']}", headers=admin_headers
        ).json()

        assert [c["name"] for c in first["items"] + rest["items"]] == ["a1", "a2", "a3"]

    def test_a_viewer_cannot_mint_one(self, client, viewer_headers, orgs):
        resp = client.put(f"{BASE}/agents", json={}, headers={**viewer_headers, **CREATE})

        assert resp.status_code == 403


def _wire(app, tmp_path) -> GitCrud:
    """A real local deploy repo carrying a deployed receiver."""
    app.state.settings.env = "dev"
    app.state.settings.deployment.target = "kubernetes"
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    gc.put(
        "helmvars",
        RECEIVER_OVERLAY,
        {"deploy": {"service": "dfe-receiver", "instance": "main"}},
        "test",
        message="test: deploy receiver",
    )
    return gc


class TestTheReceiverFollows:
    """A credential write brings the receiver's overlay into step, as a source write does."""

    def test_a_new_credential_s_binding_reaches_the_receiver_as_tenant_ids(
        self, client, app, admin_headers, orgs, tmp_path
    ):
        gc = _wire(app, tmp_path)

        resp = _create(client, admin_headers, orgs=["acme", "beta"], default_org="beta")

        assert resp.status_code == 201, resp.text
        assert resp.json()["apps_synced"] == ["dfe-receiver/main: sync routing"]
        overlay = gc.get("helmvars", RECEIVER_OVERLAY)["config"]
        assert overlay["ingest_bindings"] == {
            "credentials": {
                "agents": {"orgs": ["acme-prod", "acme-dev", "beta"], "default_org": "beta"}
            }
        }
        assert overlay["routing"]["default_org_id"] == "default"

    def test_a_deleted_credential_leaves_the_receiver(
        self, client, app, admin_headers, orgs, tmp_path
    ):
        gc = _wire(app, tmp_path)
        created = _create(client, admin_headers)

        client.delete(
            f"{BASE}/agents", headers={**admin_headers, "If-Match": created.headers["ETag"]}
        )

        assert gc.get("helmvars", RECEIVER_OVERLAY)["config"]["ingest_bindings"] == {
            "credentials": {}
        }

    def test_disabling_a_bound_org_takes_it_out_of_the_receiver_s_binding(
        self, client, app, admin_headers, orgs, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _create(client, admin_headers, orgs=["acme", "beta"], default_org="beta")

        resp = client.put("/api/v1/orgs/beta", json={"enabled": False}, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        bound = gc.get("helmvars", RECEIVER_OVERLAY)["config"]["ingest_bindings"]
        assert bound == {
            "credentials": {
                "agents": {"orgs": ["acme-prod", "acme-dev"], "default_org": "acme-prod"}
            }
        }

    def test_an_org_s_new_tenant_id_reaches_the_binding(
        self, client, app, admin_headers, orgs, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _create(client, admin_headers)

        resp = client.put("/api/v1/orgs/acme", json={"org_ids": ["acme-eu"]}, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        bound = gc.get("helmvars", RECEIVER_OVERLAY)["config"]["ingest_bindings"]
        assert bound == {"credentials": {"agents": {"orgs": ["acme-eu"], "default_org": "acme-eu"}}}


class TestTheDefaultOrg:
    def test_it_is_there_from_the_first_boot(self, client, admin_headers):
        resp = client.get("/api/v1/orgs/default", headers=admin_headers)

        assert resp.status_code == 200
        assert resp.json()["org_ids"] == ["default"]

    def test_it_survives_a_delete(self, client, admin_headers):
        resp = client.delete("/api/v1/orgs/default", headers=admin_headers)

        assert resp.status_code == 409
        assert resp.json()["code"] == "reserved_org"
        assert client.get("/api/v1/orgs/default", headers=admin_headers).status_code == 200

    def test_it_cannot_be_disabled(self, client, admin_headers):
        resp = client.put("/api/v1/orgs/default", json={"enabled": False}, headers=admin_headers)

        assert resp.status_code == 409
        assert resp.json()["code"] == "reserved_org"

    def test_it_cannot_lose_its_tenant_id(self, client, admin_headers):
        resp = client.put(
            "/api/v1/orgs/default", json={"org_ids": ["other"]}, headers=admin_headers
        )

        assert resp.status_code == 409
        assert client.get("/api/v1/orgs/default", headers=admin_headers).json()["org_ids"] == [
            "default"
        ]

    def test_no_other_org_may_take_its_tenant_id(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs", json={"name": "acme", "org_ids": ["default"]}, headers=admin_headers
        )

        assert resp.status_code == 409

    def test_an_org_another_org_s_credential_binds_is_kept(self, client, admin_headers, orgs):
        _create(client, admin_headers, orgs=["acme", "beta"])

        resp = client.delete("/api/v1/orgs/beta", headers=admin_headers)

        assert resp.status_code == 409
        assert resp.json()["code"] == "org_bound"

    def test_an_org_takes_its_own_credentials_and_their_tokens_with_it(
        self, client, app, admin_headers, orgs
    ):
        token = _create(client, admin_headers).json()["token"]

        resp = client.delete("/api/v1/orgs/acme", headers=admin_headers)

        assert resp.status_code == 204, resp.text
        assert app.state.org_registry.ingest_credentials.get("agents") is None
        assert token not in _bearer(app, "http")
        assert _secret(app, credential_secret_path("agents")) is None


class TestTheMirrorSecret:
    """With DFE_INGEST_BEARER_SECRET set, a write also reaches the Secret the receiver mounts."""

    SA_TOKEN = "service-account-token-placeholder"

    def test_a_minted_token_reaches_the_secret(self, client, app, admin_headers, orgs, tmp_path):
        token_file = tmp_path / "sa-token"
        token_file.write_text(self.SA_TOKEN, encoding="utf-8")
        with local_kube_api(self.SA_TOKEN) as kube:
            kube.create_empty()
            app.state.bearer_mirror = mirror_for(kube, token_file)

            token = _create(client, admin_headers, listeners=["http"]).json()["token"]

            assert kube.data()["http"] == f"agents:{token}\n"
            assert kube.data()["otlp"] == "\n"

    def test_a_secret_that_refuses_is_503_like_the_backend(
        self, client, app, admin_headers, orgs, tmp_path
    ):
        token_file = tmp_path / "sa-token"
        token_file.write_text(self.SA_TOKEN, encoding="utf-8")
        with local_kube_api(self.SA_TOKEN) as kube:
            app.state.bearer_mirror = mirror_for(kube, token_file)

            resp = _create(client, admin_headers, listeners=["http"])

            assert resp.status_code == 503, resp.text
            assert resp.json()["code"] == "secrets_unavailable"
            assert client.get("/api/v1/orgs", headers=admin_headers).status_code == 200

    def test_a_mirror_with_no_api_server_never_stops_startup(self, api_settings, monkeypatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.delenv("KUBERNETES_SERVICE_PORT", raising=False)
        api_settings.ingest = IngestSettings(bearer_secret="dfe-ingest-bearer-tokens")
        app = create_app(settings=api_settings)
        try:
            with TestClient(app, raise_server_exceptions=False) as started:
                assert app.state.bearer_mirror is not None
                assert started.get("/livez").status_code == 200
        finally:
            _registries.clear()
