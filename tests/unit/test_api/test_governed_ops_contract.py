"""Contract-enumeration tests: the Governed Ops closed sets exposed via the API.

The rule under test: if the server will reject values outside a set, the
contract must expose the set. Classes/resources/vars are enumerable, the
required_action default is server-derived, and an action definition can be
validated (with diff) before anything commits.
"""

import pytest

from dfe_engine.appmgmt import contract
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore


def _wire_gitcrud(app, tmp_path):
    """Attach a real local-repo GitCrud to the running app (dev posture)."""
    app.state.settings.env = "dev"
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


def _seed_helmvar(client, admin_headers, name="receiver-default", path="keda.maxReplicas", value=4):
    resp = client.put(
        f"/api/v1/helm/files/{name}/vars/{path}",
        json={"value": value},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp


class TestClassesListing:
    def test_requires_auth(self, client):
        assert client.get("/api/v1/gitops/classes").status_code == 401

    def test_503_when_gitops_not_configured(self, client, admin_headers):
        resp = client.get("/api/v1/gitops/classes", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_lists_registry_with_action_writable(self, client, app, viewer_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.get("/api/v1/gitops/classes", headers=viewer_headers)
        assert resp.status_code == 200, resp.text
        by_name = {c["name"]: c for c in resp.json()}
        # the registry's classes are all present
        assert set(by_name) == set(default_registry().names())
        # helmvars is a legal action target; every governance-prefixed class is not
        assert by_name["helmvars"]["action_writable"] is True
        assert by_name["sources"]["action_writable"] is True
        for cls in ("accounts", "groups", "roles", "actions", "policies", "gov_settings"):
            assert by_name[cls]["action_writable"] is False, cls
        # descriptor fields the UI needs
        assert by_name["helmvars"]["rbac_prefix"] == "helmvars"
        assert by_name["sources"]["versioned"] is True
        assert by_name["helmvars"]["directory"] == "values"


class TestResourceListing:
    def test_unknown_class_404(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.get("/api/v1/gitops/classes/nope/resources", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "unknown_class"

    def test_gated_by_the_class_own_read_grant(self, client, app, viewer_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        # viewer holds no helmvars:read -> the helmvars listing is forbidden
        resp = client.get("/api/v1/gitops/classes/helmvars/resources", headers=viewer_headers)
        assert resp.status_code == 403

    def test_lists_resources(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        _seed_helmvar(client, admin_headers)
        resp = client.get("/api/v1/gitops/classes/helmvars/resources", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == ["receiver-default"]


class TestVarsListing:
    def test_vars_with_protected_flag(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        _seed_helmvar(client, admin_headers, value=6)
        # protect the var, then expect the listing to mark it
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "lock", "protected": ["helmvars:*:keda.maxReplicas"]},
            headers=admin_headers,
        )
        resp = client.get(
            "/api/v1/gitops/classes/helmvars/resources/receiver-default/vars",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        by_path = {v["path"]: v for v in resp.json()}
        assert by_path["keda.maxReplicas"]["value"] == 6
        assert by_path["keda.maxReplicas"]["protected"] is True

    def test_credentials_come_back_masked(self, client, app, admin_headers, tmp_path):
        # The same deploy-repo document the helm and app routes mask.
        _wire_gitcrud(app, tmp_path)
        _seed_helmvar(client, admin_headers, value=6)
        _seed_helmvar(client, admin_headers, path="config.kafka.sasl.password", value="hunter2")
        _seed_helmvar(client, admin_headers, path="extraEnv.DFE_X_TOKEN", value="env-tok")
        resp = client.get(
            "/api/v1/gitops/classes/helmvars/resources/receiver-default/vars",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        by_path = {v["path"]: v["value"] for v in resp.json()}
        assert by_path["config.kafka.sasl.password"] == contract.REDACTED
        assert by_path["extraEnv.DFE_X_TOKEN"] == contract.REDACTED
        assert by_path["keda.maxReplicas"] == 6
        assert "hunter2" not in resp.text
        assert "env-tok" not in resp.text

    def test_missing_resource_404(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.get(
            "/api/v1/gitops/classes/helmvars/resources/absent/vars",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_gated_by_the_class_own_read_grant(self, client, app, viewer_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.get(
            "/api/v1/gitops/classes/helmvars/resources/receiver-default/vars",
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestRequiredActionDefault:
    def test_default_is_derived_from_the_name(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        action = {
            "name": "surge",
            "description": "raise the ceiling",
            "changes": [
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.maxReplicas",
                    "value": 12,
                }
            ],
        }
        created = client.post(
            "/api/v1/governance/admin/actions", json=action, headers=admin_headers
        )
        assert created.status_code == 201, created.text
        assert created.json()["required_action"] == "action:invoke:surge"
        stored = client.get("/api/v1/governance/actions/surge", headers=admin_headers)
        assert stored.json()["required_action"] == "action:invoke:surge"

    def test_explicit_value_is_kept(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        action = {
            "name": "surge",
            "required_action": "action:invoke:custom-handle",
            "changes": [],
        }
        created = client.post(
            "/api/v1/governance/admin/actions", json=action, headers=admin_headers
        )
        assert created.status_code == 201, created.text
        assert created.json()["required_action"] == "action:invoke:custom-handle"


def _valid_action():
    return {
        "name": "surge",
        "description": "raise the ceiling",
        "changes": [
            {
                "cls": "helmvars",
                "name": "receiver-default",
                "path": "keda.maxReplicas",
                "value": 12,
            }
        ],
    }


class TestValidateDefine:
    def test_valid_action_returns_diff_without_committing(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        _seed_helmvar(client, admin_headers, value=4)
        head_before = gc.head_revision()
        resp = client.post(
            "/api/v1/governance/admin/actions/validate",
            json=_valid_action(),
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is True
        assert body["errors"] == []
        assert body["diff"] == [
            {
                "cls": "helmvars",
                "name": "receiver-default",
                "path": "keda.maxReplicas",
                "old": 4,
                "new": 12,
            }
        ]
        # nothing was written: HEAD unmoved, action not defined
        assert gc.head_revision() == head_before
        listed = client.get("/api/v1/governance/actions", headers=admin_headers)
        assert listed.json() == []

    def test_collects_every_error(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        action = {
            "name": "bad",
            "changes": [
                {"cls": "nope", "name": "x", "path": "a.b", "value": 1},
                {"cls": "roles", "name": "admin", "path": "permissions", "value": ["*"]},
                {"cls": "helmvars", "name": "receiver-default", "path": "replicaCount", "value": 3},
            ],
        }
        resp = client.post(
            "/api/v1/governance/admin/actions/validate", json=action, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is False
        # one error per broken change, all reported in one pass
        assert len(body["errors"]) == 3
        joined = " ".join(body["errors"])
        assert "nope" in joined
        assert "governance" in joined
        assert "replicaCount" in joined

    def test_protected_var_reported_without_override(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        from tests.unit.test_api.test_governed_ops import _scoped_headers

        _wire_gitcrud(app, tmp_path)
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "lock", "protected": ["helmvars:*:keda.maxReplicas"]},
            headers=admin_headers,
        )
        gov_writer = _scoped_headers(
            app,
            api_settings,
            username="govw",
            role="gov-writer",
            permissions=["governance:read", "governance:write"],  # NO helmvars:override
        )
        resp = client.post(
            "/api/v1/governance/admin/actions/validate",
            json=_valid_action(),
            headers=gov_writer,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is False
        assert any("protected" in e for e in body["errors"])

    def test_requires_governance_write(self, client, app, viewer_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.post(
            "/api/v1/governance/admin/actions/validate",
            json=_valid_action(),
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestInvokeParamsAPI:
    def _define_surge(self, client, admin_headers):
        action = {
            "name": "receiver-surge",
            "description": "raise the ceiling",
            "params": {"level": {"type": "enum", "values": ["2x", "max"], "default": "2x"}},
            "changes": [
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.maxReplicas",
                    "value": {"$param": "level", "map": {"2x": 8, "max": 20}},
                }
            ],
        }
        created = client.post(
            "/api/v1/governance/admin/actions", json=action, headers=admin_headers
        )
        assert created.status_code == 201, created.text

    def test_invoke_with_params(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        self._define_surge(client, admin_headers)
        res = client.post(
            "/api/v1/governance/actions/receiver-surge/invoke",
            json={"params": {"level": "max"}},
            headers=admin_headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["changed"] is True
        assert res.json()["diff"][0]["new"] == 20
        assert gc.get("helmvars", "receiver-default")["keda"]["maxReplicas"] == 20

    def test_invoke_without_body_uses_defaults(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        self._define_surge(client, admin_headers)
        res = client.post("/api/v1/governance/actions/receiver-surge/invoke", headers=admin_headers)
        assert res.status_code == 200, res.text
        assert gc.get("helmvars", "receiver-default")["keda"]["maxReplicas"] == 8

    def test_invalid_param_is_422(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        self._define_surge(client, admin_headers)
        res = client.post(
            "/api/v1/governance/actions/receiver-surge/invoke",
            json={"params": {"level": "11x"}},
            headers=admin_headers,
        )
        assert res.status_code == 422, res.text
        assert res.json()["code"] == "invalid_params"

    def test_unconstrained_param_cannot_be_defined(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        action = {
            "name": "bad",
            "params": {"free": {"type": "str"}},
            "changes": [],
        }
        res = client.post("/api/v1/governance/admin/actions", json=action, headers=admin_headers)
        assert res.status_code == 422


class TestActionDiffsMaskCredentials:
    """An action's diff goes back to its caller, so a credential in it is masked."""

    PATH = "config.kafka.sasl.password"

    def _action(self, value, name: str = "rotate-kafka") -> dict:
        return {
            "name": name,
            "description": "rotate the bus credential",
            "changes": [
                {"cls": "helmvars", "name": "receiver-default", "path": self.PATH, "value": value},
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.maxReplicas",
                    "value": 12,
                },
            ],
        }

    def _seed(self, client, admin_headers):
        _seed_helmvar(client, admin_headers, value=4)
        _seed_helmvar(client, admin_headers, path=self.PATH, value="old-pw")

    def test_the_validate_diff_masks_old_and_new(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        self._seed(client, admin_headers)
        resp = client.post(
            "/api/v1/governance/admin/actions/validate",
            json=self._action("new-pw"),
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        by_path = {d["path"]: d for d in resp.json()["diff"]}
        assert (by_path[self.PATH]["old"], by_path[self.PATH]["new"]) == (
            contract.REDACTED,
            contract.REDACTED,
        )
        assert (by_path["keda.maxReplicas"]["old"], by_path["keda.maxReplicas"]["new"]) == (4, 12)
        assert "old-pw" not in resp.text
        assert "new-pw" not in resp.text

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_the_invoke_diff_masks_and_the_write_keeps_the_value(
        self, client, app, admin_headers, tmp_path, dry_run
    ):
        gc = _wire_gitcrud(app, tmp_path)
        self._seed(client, admin_headers)
        created = client.post(
            "/api/v1/governance/admin/actions", json=self._action("new-pw"), headers=admin_headers
        )
        assert created.status_code == 201, created.text
        resp = client.post(
            f"/api/v1/governance/actions/rotate-kafka/invoke?dry_run={str(dry_run).lower()}",
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        by_path = {d["path"]: d for d in resp.json()["diff"]}
        assert by_path[self.PATH]["new"] == contract.REDACTED
        assert "old-pw" not in resp.text
        assert "new-pw" not in resp.text
        stored = gc.get("helmvars", "receiver-default")["config"]["kafka"]["sasl"]["password"]
        assert stored == ("old-pw" if dry_run else "new-pw")

    def test_a_masked_action_value_keeps_the_stored_credential(
        self, client, app, admin_headers, tmp_path
    ):
        # An action authored from a masked listing carries the mask as its value.
        gc = _wire_gitcrud(app, tmp_path)
        self._seed(client, admin_headers)
        client.post(
            "/api/v1/governance/admin/actions",
            json=self._action(contract.REDACTED),
            headers=admin_headers,
        )
        resp = client.post("/api/v1/governance/actions/rotate-kafka/invoke", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        stored = gc.get("helmvars", "receiver-default")
        assert stored["config"]["kafka"]["sasl"]["password"] == "old-pw"
        assert stored["keda"]["maxReplicas"] == 12

    def test_the_mask_with_nothing_behind_it_is_refused(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        _seed_helmvar(client, admin_headers, value=4)
        validated = client.post(
            "/api/v1/governance/admin/actions/validate",
            json=self._action(contract.REDACTED),
            headers=admin_headers,
        ).json()
        assert validated["valid"] is False
        assert any("nothing is stored" in e for e in validated["errors"])

        client.post(
            "/api/v1/governance/admin/actions",
            json=self._action(contract.REDACTED),
            headers=admin_headers,
        )
        before = gc.head_revision()
        resp = client.post("/api/v1/governance/actions/rotate-kafka/invoke", headers=admin_headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "masked_value"
        assert gc.head_revision() == before


class TestEnumSourceAnnotations:
    def test_varchange_fields_carry_enum_sources(self, client):
        spec = client.get("/openapi.json").json()
        props = spec["components"]["schemas"]["VarChange"]["properties"]
        assert props["cls"]["x-dfe-enum-source"] == {
            "endpoint": "/api/v1/gitops/classes",
            "value_key": "name",
        }
        assert props["name"]["x-dfe-enum-source"] == {
            "endpoint": "/api/v1/gitops/classes/{cls}/resources",
            "params": {"cls": "cls"},
        }
        assert props["path"]["x-dfe-enum-source"] == {
            "endpoint": "/api/v1/gitops/classes/{cls}/resources/{name}/vars",
            "params": {"cls": "cls", "name": "name"},
            "value_key": "path",
        }

    def test_required_action_documents_the_default(self, client):
        spec = client.get("/openapi.json").json()
        schema = spec["components"]["schemas"]["ActionDef"]
        assert "required_action" not in schema.get("required", [])
