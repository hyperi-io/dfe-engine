#  Project:      dfe-engine
#  File:         tests/gitcrud/test_routing.py
#  Purpose:      Direct-vs-PR write routing over a real repo + bare remote
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""route_write: dev/solo commit to main; production+team goes to a review PR.

Real dulwich repos throughout: a bare remote stands in for the deploy repo, so the
side-branch push is a genuine git operation. The forge (an EXTERNAL service) is a
small recording double at the ForgeProvider seam; its real REST behaviour is
covered in test_forge.py.
"""

from __future__ import annotations

import pytest
from dulwich import porcelain
from dulwich.repo import Repo

from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError, default_registry
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitcrud.routing import ReviewRequiredError, route_write
from dfe_engine.gitops.repo import GitopsRepo


class _RecordingForge:
    """A ForgeProvider seam double that records the PR it was asked to open."""

    def __init__(self, url: str = "http://forge/pr/1") -> None:
        self.calls: list[dict] = []
        self._url = url

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        self.calls.append({"head": head, "base": base, "title": title, "body": body})
        return PullRequest(number=1, url=self._url, branch=head)


def _seed_bare(tmp_path):
    """A bare remote seeded with a ``main`` branch (mirrors a real deploy repo)."""
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


def _remote_main(bare) -> str:
    with Repo(str(bare)) as r:
        return r.refs[b"refs/heads/main"].decode()


def _remote_refs(bare) -> list[str]:
    with Repo(str(bare)) as r:
        return sorted(k.decode() for k in r.refs.allkeys())


@pytest.fixture
def wired(tmp_path):
    """GitCrud over a real clone of a seeded bare remote (push enabled)."""
    bare = _seed_bare(tmp_path)
    repo = GitopsRepo(
        local_path=str(tmp_path / "work"), repo_url=str(bare), push=True, branch="main"
    )
    repo.ensure()
    return GitCrud(repo, default_registry()), bare


def _set_helm(gc: GitCrud, value: int):
    return lambda branch: gc.set_key(
        "helmvars", "receiver-default", "keda.maxReplicas", value, "alice", branch=branch
    )


def test_dev_posture_commits_direct_to_main(wired):
    gc, bare = wired
    before = _remote_main(bare)
    outcome = route_write(
        gc=gc,
        forge=None,  # dev never needs a forge
        environment="dev",
        mode="team",
        rbac_class="helmvars",
        resource="helmvars/receiver-default:keda.maxReplicas",
        actor="alice",
        title="cfg(receiver-default): set",
        body="why",
        write=_set_helm(gc, 10),
    )
    assert outcome.changed is True
    assert outcome.review_required is False
    assert outcome.branch is None
    # applied on main, and the remote main moved
    assert gc.get("helmvars", "receiver-default")["keda"]["maxReplicas"] == 10
    assert _remote_main(bare) != before


def test_prod_team_routes_to_review_pr(wired):
    gc, bare = wired
    main_before = _remote_main(bare)
    forge = _RecordingForge(url="http://forge/pr/42")

    outcome = route_write(
        gc=gc,
        forge=forge,
        environment="production",
        mode="team",
        rbac_class="helmvars",
        resource="helmvars/receiver-default:keda.maxReplicas",
        actor="alice",
        title="cfg(receiver-default): set keda.maxReplicas",
        body="please review",
        write=_set_helm(gc, 10),
    )

    # the change was routed to a PR, not committed to main
    assert outcome.review_required is True
    assert outcome.pr_url == "http://forge/pr/42"
    assert outcome.branch.startswith("dfe/helmvars/")
    assert outcome.changed is True

    # the forge was asked to open exactly one PR from the branch onto main
    assert len(forge.calls) == 1
    assert forge.calls[0]["base"] == "main"
    assert forge.calls[0]["head"] == outcome.branch

    # main is untouched: locally the value never landed, remote main did not move
    with pytest.raises(ResourceNotFoundError):
        gc.get("helmvars", "receiver-default")
    assert _remote_main(bare) == main_before
    # the review branch really was pushed to the remote
    assert f"refs/heads/{outcome.branch}" in _remote_refs(bare)


def test_prod_team_refused_without_forge(wired):
    gc, bare = wired
    main_before = _remote_main(bare)
    with pytest.raises(ReviewRequiredError):
        route_write(
            gc=gc,
            forge=None,
            environment="production",
            mode="team",
            rbac_class="helmvars",
            resource="helmvars/receiver-default:keda.maxReplicas",
            actor="alice",
            title="cfg(receiver-default): set",
            body="why",
            write=_set_helm(gc, 10),
        )
    # nothing written anywhere
    with pytest.raises(ResourceNotFoundError):
        gc.get("helmvars", "receiver-default")
    assert _remote_main(bare) == main_before
    assert _remote_refs(bare) == ["HEAD", "refs/heads/main"]


def test_prod_team_solo_posture_commits_direct(wired):
    gc, bare = wired
    # solo posture: the gate permits direct commit even in production
    outcome = route_write(
        gc=gc,
        forge=None,
        environment="production",
        mode="solo",
        rbac_class="helmvars",
        resource="helmvars/receiver-default:keda.maxReplicas",
        actor="alice",
        title="cfg(receiver-default): set",
        body="why",
        write=_set_helm(gc, 5),
    )
    assert outcome.changed is True
    assert outcome.review_required is False
    assert gc.get("helmvars", "receiver-default")["keda"]["maxReplicas"] == 5


def test_prod_team_noop_write_opens_no_pr(wired):
    gc, bare = wired
    # seed the value on main directly, then a routed write of the SAME value is a
    # no-op: nothing to review, so no branch is pushed and no PR is opened.
    gc.set_key("helmvars", "receiver-default", "keda.maxReplicas", 10, "seed")
    refs_before = _remote_refs(bare)
    forge = _RecordingForge()
    outcome = route_write(
        gc=gc,
        forge=forge,
        environment="production",
        mode="team",
        rbac_class="helmvars",
        resource="helmvars/receiver-default:keda.maxReplicas",
        actor="alice",
        title="cfg(receiver-default): set",
        body="why",
        write=_set_helm(gc, 10),
    )
    assert outcome.changed is False
    assert outcome.review_required is False
    assert forge.calls == []
    assert _remote_refs(bare) == refs_before
