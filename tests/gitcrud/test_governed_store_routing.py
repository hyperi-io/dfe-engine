#  Project:      dfe-engine
#  File:         tests/gitcrud/test_governed_store_routing.py
#  Purpose:      Sigma and ingest writes take the review path in production+team
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A production+team sigma or ingest write lands on a review branch, never main.

Each store is built as the API builds it with no posture, so the loaded settings
decide. Real dulwich repos with a bare remote stand in for the deploy repo.
"""

from collections.abc import Callable
from typing import Any

import pytest
from dulwich import porcelain
from dulwich.refs import Ref
from dulwich.repo import Repo

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.ingest.models import CompatSetting, RuleOrder
from dfe_engine.ingest.store import IngestStore
from dfe_engine.sigma.catalog import (
    SigmaCatalogStore,
    SigmaProviderStore,
    SigmaSelectionStore,
    default_provider_configs,
)
from dfe_engine.sigma.views import SigmaViewDefinition, SigmaViewStore


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


@pytest.fixture
def posture(monkeypatch, request):
    """The loaded settings in the posture the test is parametrised with."""
    from dfe_engine.settings import reset_settings

    environment = request.param
    monkeypatch.setenv("DFE_ENV", environment)
    monkeypatch.setenv("DFE_GITOPS_MODE", "team")
    if environment == "production":
        monkeypatch.setenv("DFE_AUTH_ENABLED", "true")
        monkeypatch.setenv("DFE_API_JWT_SECRET", "routing-test-" + "x" * 40)
    reset_settings()
    yield environment
    reset_settings()


_RULE = {"title": "Certutil Abuse", "logsource": {"product": "windows"}, "detection": {}}


def _view() -> SigmaViewDefinition:
    return SigmaViewDefinition.model_validate({"source_name": "winlog", "columns": []})


# Each writer: (store, the write) for one store method a route calls.
WRITERS: dict[str, Callable[[GitCrud], Any]] = {
    "sigma rule edit": lambda gc: SigmaCatalogStore(gc).edit_rule("r1", _RULE, "alice"),
    "sigma selection": lambda gc: SigmaSelectionStore(gc).select("r1", "alice"),
    "sigma provider": lambda gc: SigmaProviderStore(gc).save_config(
        default_provider_configs()[0], "alice"
    ),
    "sigma view": lambda gc: SigmaViewStore(gc).save(_view(), "alice"),
    "ingest compat": lambda gc: IngestStore(gc).put_compat(
        CompatSetting(), "alice", base_revision=None
    ),
    "ingest order": lambda gc: IngestStore(gc).put_order(RuleOrder(), "alice", base_revision=None),
}


def _seed_rule(gc: GitCrud) -> None:
    """The rule the edit case changes, stored on main before the posture under test."""
    from dfe_engine.sigma.catalog import RULES_CLASS, _sigma_crud

    _sigma_crud(gc).put(RULES_CLASS, "r1", {"id": "r1", "rule": {}, "provenance": {}}, "seed")


@pytest.mark.parametrize("posture", ["production"], indirect=True)
@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_a_production_team_write_lands_on_a_review_branch(wired, posture, writer):
    gc, bare = wired
    _seed_rule(gc)
    before = _remote_main(bare)

    WRITERS[writer](gc)

    assert _remote_main(bare) == before, "the write reached main"
    assert len(_review_branches(bare)) == 1


@pytest.mark.parametrize("posture", ["dev"], indirect=True)
@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_a_dev_write_still_lands_on_main(wired, posture, writer):
    gc, bare = wired
    _seed_rule(gc)
    before = _remote_main(bare)

    WRITERS[writer](gc)

    assert _remote_main(bare) != before
    assert _review_branches(bare) == []
