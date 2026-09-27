#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_gitops_unavailable.py
#  Purpose:      A write the deploy repo never answers is a 503, not a 500
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""An unreachable deploy repo is a backing service being down, and says so.

Real git throughout: the clone is taken from a bare remote over its path, then
pointed at a loopback port nothing listens on, so every fetch the write makes is
refused. The API must answer that 503 with Retry-After and name the remote, and the
exhausted write must show on the metrics the engine serves.
"""

import socket

from dulwich import porcelain
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.metrics import GitopsMetrics
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore


def _seeded_bare(tmp_path):
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


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _sample(manager, name: str, labels: dict[str, str]) -> float:
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


def test_an_unreachable_deploy_repo_answers_503_with_retry_after(
    client, app, admin_headers, tmp_path, monkeypatch
):
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    bare = _seeded_bare(tmp_path)
    work = tmp_path / "work"
    porcelain.clone(str(bare), str(work), branch=b"main")
    remote = f"http://127.0.0.1:{_free_port()}/deploy.git"
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    repo = GitopsRepo(
        local_path=str(work),
        repo_url=remote,
        branch="main",
        push=True,
        metrics=GitopsMetrics(manager),
    )
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    app.state.settings.gitops.mode = "solo"

    resp = client.put("/api/v1/gitops/auto-merge", json={"enabled": True}, headers=admin_headers)

    assert resp.status_code == 503, resp.text
    assert int(resp.headers["Retry-After"]) >= 1
    body = resp.json()
    assert body["code"] == "service_unavailable"
    assert body["context"] == {"service": "gitops", "remote": remote}
    assert "gitops deploy repo" in body["message"]
    labels = {"op": "fetch", "outcome": "exhausted"}
    assert _sample(manager, "gitops_write_retries_total", labels) == 1
