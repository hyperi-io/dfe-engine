"""GET/PUT /api/v1/gitops/auto-merge - the UI's auto-merge contract.

Also covers the production+team WRITE-GATE enforcement: with auto-merge refused,
a governed write must NOT land on main - it is routed to a review PR, or refused
(409) when no forge is configured (Finding 5).
"""

import pytest
from dulwich import porcelain
from dulwich.object_store import tree_lookup_path
from dulwich.objects import Blob, Commit
from dulwich.refs import local_branch_name
from dulwich.repo import Repo

from dfe_engine.appmgmt import contract
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitcrud.engine import get_path
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore
from dfe_engine.yaml_utils import yaml_load_string


def _wire_gitcrud(app, tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


class _RecordingForge:
    """ForgeProvider seam double: records the PR the endpoint opens."""

    def __init__(self, url: str = "http://forge/pr/9") -> None:
        self.calls: list[dict] = []
        self._url = url

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        self.calls.append({"head": head, "base": base, "title": title, "body": body})
        return PullRequest(number=9, url=self._url, branch=head)


def _seed_bare(tmp_path):
    """A bare remote seeded with main - a stand-in deploy repo (real git)."""
    bare = tmp_path / "remote.git"
    porcelain.init(str(bare), bare=True)
    seed = tmp_path / "seed"
    porcelain.init(str(seed))
    (seed / "README.md").write_text("seed\n")
    porcelain.add(str(seed), paths=[str(seed / "README.md")])
    porcelain.commit(str(seed), message=b"seed", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), str(bare), f"refs/heads/{branch}:refs/heads/main".encode())
    return bare


def _wire_gitcrud_remote(app, tmp_path, forge):
    """Wire a push-enabled GitCrud over a real bare remote, plus a forge."""
    bare = _seed_bare(tmp_path)
    repo = GitopsRepo(
        local_path=str(tmp_path / "work"), repo_url=str(bare), push=True, branch="main"
    )
    repo.ensure()
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    app.state.forge = forge
    return gc, bare


class TestAutoMergeApi:
    def test_503_when_gitops_not_configured(self, client, admin_headers):
        resp = client.get("/api/v1/gitops/auto-merge", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_get_default_prod_team(self, client, app, admin_headers, tmp_path):
        # test settings default: env=production, gitops.mode=team
        _wire_gitcrud(app, tmp_path)
        resp = client.get("/api/v1/gitops/auto-merge", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "stored": False,
            "effective": False,
            "allowed": False,
            "reason": body["reason"],
        }
        assert "DFE_GITOPS_MODE" in body["reason"]

    def test_enable_refused_in_prod_team(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": True}, headers=admin_headers
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "auto_merge_forbidden"

    def test_enable_allowed_in_solo(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        app.state.settings.gitops.mode = "solo"
        resp = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": True}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stored"] is True
        assert body["effective"] is True
        # the toggle is itself a gitops commit
        assert gc.get("gov_settings", "gitops")["auto_merge"] is True

    def test_disable_always_allowed(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        app.state.settings.gitops.mode = "solo"
        client.put("/api/v1/gitops/auto-merge", json={"enabled": True}, headers=admin_headers)
        # flip the deployment back to team: stored ON, gate refuses
        app.state.settings.gitops.mode = "team"
        got = client.get("/api/v1/gitops/auto-merge", headers=admin_headers)
        assert got.json()["stored"] is True
        assert got.json()["effective"] is False
        # turning OFF must still work
        off = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": False}, headers=admin_headers
        )
        assert off.status_code == 200
        assert off.json()["stored"] is False

    def test_viewer_cannot_toggle(self, client, app, viewer_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        app.state.settings.gitops.mode = "solo"
        resp = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": True}, headers=viewer_headers
        )
        assert resp.status_code == 403


class TestAutoMergedBadge:
    def _enable(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        app.state.settings.gitops.mode = "solo"
        resp = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": True}, headers=admin_headers
        )
        assert resp.status_code == 200
        return gc

    def test_helm_write_badged_in_prod_solo(self, client, app, admin_headers, tmp_path):
        # env=production -> helmvars write would be PR mode -> badge when ON
        self._enable(client, app, admin_headers, tmp_path)
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["auto_merged"] is True

    def test_prod_team_write_refused_without_forge(self, client, app, admin_headers, tmp_path):
        # Finding 5: default posture is production+team (auto-merge refused). With
        # no forge configured the write may NOT hit main -> 409, nothing committed.
        gc = _wire_gitcrud(app, tmp_path)  # push=False, no forge on app.state
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "review_required"
        # the deploy repo was never written
        assert gc.list("helmvars") == []
        assert gc.head_revision() is None

    def test_noop_write_not_badged_or_warned(self, client, app, admin_headers, tmp_path):
        # auto-merge effective-ON: a repeat write with the SAME value is a no-op
        # commit (res.changed False) - must not badge, must not WARN.
        self._enable(client, app, admin_headers, tmp_path)
        first = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert first.status_code == 200, first.text
        second = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["changed"] is False
        assert body["auto_merged"] is False

    def test_action_invoke_badged(self, client, app, admin_headers, tmp_path):
        # governance class always resolves to PR mode -> badge when ON
        self._enable(client, app, admin_headers, tmp_path)
        action = {
            "name": "scale-receiver",
            "description": "scale receiver",
            "required_action": "action:invoke:scale-receiver",
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
        res = client.post("/api/v1/governance/actions/scale-receiver/invoke", headers=admin_headers)
        assert res.status_code == 200, res.text
        assert res.json()["auto_merged"] is True

    def test_create_action_succeeds_with_auto_merge_on(self, client, app, admin_headers, tmp_path):
        # spec: the conversion WARN extends to admin CRUD too - create_action still
        # succeeds (201) when auto-merge is ON and would otherwise have been PR-mode.
        self._enable(client, app, admin_headers, tmp_path)
        action = {
            "name": "scale-loader",
            "description": "scale loader",
            "required_action": "action:invoke:scale-loader",
            "changes": [
                {
                    "cls": "helmvars",
                    "name": "loader-default",
                    "path": "keda.maxReplicas",
                    "value": 9,
                }
            ],
        }
        created = client.post(
            "/api/v1/governance/admin/actions", json=action, headers=admin_headers
        )
        assert created.status_code == 201, created.text


class TestProdTeamPrRouting:
    """Production+team: the write is routed to a real review PR, not to main."""

    def _remote_main(self, bare) -> str:
        with Repo(str(bare)) as r:
            return r.refs[local_branch_name(b"main")].decode()

    def _remote_refs(self, bare) -> list[str]:
        with Repo(str(bare)) as r:
            return sorted(k.decode() for k in r.refs.allkeys())

    def test_helm_write_opens_pr_and_leaves_main(self, client, app, admin_headers, tmp_path):
        forge = _RecordingForge(url="http://forge/pr/42")
        gc, bare = _wire_gitcrud_remote(app, tmp_path, forge)
        main_before = self._remote_main(bare)

        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["review_required"] is True
        assert body["pr_url"] == "http://forge/pr/42"
        assert body["auto_merged"] is False

        # forge asked to open one PR from the pushed branch onto main
        assert len(forge.calls) == 1
        branch = forge.calls[0]["head"]
        assert forge.calls[0]["base"] == "main"
        assert branch.startswith("dfe/helmvars/")

        # main untouched; the review branch really landed on the remote
        assert self._remote_main(bare) == main_before
        assert f"refs/heads/{branch}" in self._remote_refs(bare)

    def _branch_doc(self, bare, gc, branch: str) -> dict:
        """The resource as the review branch carries it on the remote."""
        rel = gc._rel(gc._cls("helmvars"), "receiver-default").encode()
        with Repo(str(bare)) as r:
            commit = r[r.refs[local_branch_name(branch.encode())]]
            assert isinstance(commit, Commit)
            _mode, sha = tree_lookup_path(r.__getitem__, commit.tree, rel)
            blob = r[sha]
            assert isinstance(blob, Blob)
            return yaml_load_string(blob.data.decode())

    @pytest.mark.parametrize(
        ("path", "credential"),
        [
            ("config.kafka.sasl.password", "hunter2"),
            ("extraEnv.DFE_X_API_KEY", "env-key"),
            ("auth.bearer-tokens", "chart-tok-4410"),
            ("minio.rootPassword", "minio-pw-4431"),
            ("extraEnv.PGPASSWORD", "pg-pw-4432"),
        ],
    )
    def test_a_credential_is_named_not_shown_in_the_pr(
        self, client, app, admin_headers, tmp_path, path, credential
    ):
        # The PR text leaves the deploy repo for the forge; the branch does not.
        forge = _RecordingForge()
        gc, bare = _wire_gitcrud_remote(app, tmp_path, forge)
        resp = client.put(
            f"/api/v1/helm/files/receiver-default/vars/{path}",
            json={"value": credential},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert credential not in resp.text

        body = forge.calls[0]["body"]
        assert credential not in body
        assert f"{path}={contract.REDACTED!r}" in body
        written = self._branch_doc(bare, gc, forge.calls[0]["head"])
        assert get_path(written, path) == credential

    def test_a_plain_value_is_still_shown_in_the_pr(self, client, app, admin_headers, tmp_path):
        forge = _RecordingForge()
        _wire_gitcrud_remote(app, tmp_path, forge)
        client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert "keda.maxReplicas=10" in forge.calls[0]["body"]

    def test_admin_create_action_opens_pr_with_header(self, client, app, admin_headers, tmp_path):
        forge = _RecordingForge(url="http://forge/pr/7")
        gc, bare = _wire_gitcrud_remote(app, tmp_path, forge)
        action = {
            "name": "scale-receiver",
            "description": "scale receiver",
            "required_action": "action:invoke:scale-receiver",
            "changes": [
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.maxReplicas",
                    "value": 12,
                }
            ],
        }
        # defining the action is a governance-class write -> routed to a PR; the
        # 201 body is unchanged but the review PR is signalled via response headers
        created = client.post(
            "/api/v1/governance/admin/actions", json=action, headers=admin_headers
        )
        assert created.status_code == 201, created.text
        assert created.headers.get("X-DFE-Review-Required") == "true"
        assert created.headers.get("X-DFE-PR-Url") == "http://forge/pr/7"
        assert len(forge.calls) == 1
        # the action def is not on main yet (it is in a PR), so a later invoke 404s
        assert gc.list("actions") == []


class TestNameValidationApi:
    def test_bad_resource_name_rejected(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        # 'a..b' is a valid URL segment but carries '..' -> refused at the router
        resp = client.put(
            "/api/v1/helm/files/a..b/vars/keda.maxReplicas",
            json={"value": 1},
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "invalid_name"
