#  Project:      dfe-engine
#  File:         tests/gitcrud/test_source_review_routing.py
#  Purpose:      Source and derived-schema writes take the review path in production+team
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A production+team source or derived-schema write lands on a review branch, never main.

Real dulwich repos with a bare remote standing in for the deploy repo, as in
``test_routing.py``. The forge is the recording double at the ForgeProvider seam.
"""

import pytest
from dulwich import porcelain
from dulwich.refs import Ref
from dulwich.repo import Repo

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitcrud.routing import WritePosture
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.schema.derived import DerivedSchema
from dfe_engine.schema.derived_registry import DerivedSchemaRegistry, derived_reference
from dfe_engine.source.models import Source, SourceMatch, SourceView
from dfe_engine.source.registry import SourceRegistry


class _RecordingForge:
    def __init__(self) -> None:
        self.heads: list[str] = []

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        self.heads.append(head)
        return PullRequest(
            number=len(self.heads), url=f"http://forge/pr/{len(self.heads)}", branch=head
        )


def _seed_bare(tmp_path):
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
        return r.refs[Ref(b"refs/heads/main")].decode()


def _review_branches(bare) -> list[str]:
    with Repo(str(bare)) as r:
        return sorted(k.decode() for k in r.refs.allkeys() if k.startswith(b"refs/heads/dfe/"))


@pytest.fixture
def wired(tmp_path):
    bare = _seed_bare(tmp_path)
    repo = GitopsRepo(
        local_path=str(tmp_path / "work"), repo_url=str(bare), push=True, branch="main"
    )
    repo.ensure()
    return GitCrud(repo, default_registry()), bare


def _source() -> Source:
    return Source(  # ty: ignore[pydantic-discarded-extra-argument]  # Source lifts its flat fields into the version tree in a before-validator
        source="filebeat",
        enabled=True,
        description="filebeat test source",
        match=SourceMatch(field="tags.collector.type", value="filebeat"),
        views=[SourceView(standard="sigma", taxonomy="linux")],
    )


def _derived() -> DerivedSchema:
    block = {
        "date": "2026-09-21",
        "summary": "system.auth subset",
        "select": [{"name": "timestamp"}, {"name": "host_name", "index": "exact_match"}],
    }
    return DerivedSchema.model_validate(
        {
            "base": "meta/beats/filebeat",
            "base_version": "1.0.0",
            "current": "1.0.0",
            "versions": {"1.0.0": block},
            "path": derived_reference("beats/filebeat_auth"),
        }
    )


TEAM = {"environment": "production", "mode": "team"}


def _writes(gc, posture):
    sources = SourceRegistry(crud=gc, posture=posture)
    derived = DerivedSchemaRegistry(crud=gc, posture=posture)
    return {
        "source save": lambda: sources.save_source(_source(), created_by="alice"),
        "derived save": lambda: derived.save(_derived(), created_by="alice"),
    }


@pytest.mark.parametrize("write", ["source save", "derived save"])
@pytest.mark.parametrize("forge", [True, False], ids=["forge", "no forge"])
def test_a_production_team_write_lands_on_a_review_branch(wired, write, forge):
    gc, bare = wired
    recording = _RecordingForge() if forge else None
    before = _remote_main(bare)

    _writes(gc, WritePosture(**TEAM, forge=recording))[write]()

    assert _remote_main(bare) == before
    assert len(_review_branches(bare)) == 1
    if recording is not None:
        assert len(recording.heads) == 1


@pytest.mark.parametrize("write", ["source save", "derived save"])
def test_a_dev_write_still_lands_on_main(wired, write):
    gc, bare = wired
    before = _remote_main(bare)

    _writes(gc, WritePosture(environment="dev", mode="team"))[write]()

    assert _remote_main(bare) != before
    assert _review_branches(bare) == []


def test_a_production_team_delete_lands_on_a_review_branch(wired):
    gc, bare = wired
    SourceRegistry(crud=gc, posture=WritePosture(environment="dev", mode="team")).save_source(
        _source()
    )
    derived_dev = DerivedSchemaRegistry(
        crud=gc, posture=WritePosture(environment="dev", mode="team")
    )
    derived_dev.save(_derived())
    before = _remote_main(bare)
    team = WritePosture(**TEAM, forge=_RecordingForge())

    SourceRegistry(crud=gc, posture=team).delete_source("filebeat", created_by="alice")
    DerivedSchemaRegistry(crud=gc, posture=team).delete("beats/filebeat_auth")

    assert _remote_main(bare) == before
    assert len(_review_branches(bare)) == 2


def test_a_registry_given_no_posture_never_commits_to_main_in_production(wired, monkeypatch):
    """With no posture passed, the loaded settings decide, and no forge means a branch."""
    gc, bare = wired
    monkeypatch.setenv("DFE_ENV", "production")
    monkeypatch.setenv("DFE_GITOPS_MODE", "team")
    monkeypatch.setenv("DFE_AUTH_ENABLED", "true")
    monkeypatch.setenv("DFE_API_JWT_SECRET", "routing-test-" + "x" * 40)
    from dfe_engine.settings import reset_settings

    reset_settings()
    before = _remote_main(bare)
    try:
        SourceRegistry(crud=gc).save_source(_source())
    finally:
        reset_settings()

    assert _remote_main(bare) == before
    assert len(_review_branches(bare)) == 1


@pytest.mark.parametrize("write", ["save", "delete"])
def test_an_engine_owned_write_commits_to_main_in_production_team(wired, write):
    """Core-source reconciles are the engine's, not an operator's edit, so they never wait on review.

    Routed, every start would open another review branch for the landing source and
    main would never carry it.
    """
    gc, bare = wired
    team = SourceRegistry(crud=gc, posture=WritePosture(**TEAM, forge=_RecordingForge()))
    if write == "delete":
        team.save_source(_source(), created_by="engine", core_reconcile=True)
    before = _remote_main(bare)

    if write == "save":
        team.save_source(_source(), created_by="engine", core_reconcile=True)
    else:
        team.delete_source("filebeat", created_by="engine", core_reconcile=True)

    assert _remote_main(bare) != before
    assert _review_branches(bare) == []
